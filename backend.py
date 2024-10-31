import os
from flask import Flask, request, jsonify, send_file
from werkzeug.utils import secure_filename
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from io import BytesIO
import uuid
import datetime
from pymongo import MongoClient
from functools import wraps
from auth0.v3.authentication import GetToken
from auth0.v3.management import Auth0
import PyPDF2
from PIL import Image
import pytesseract
import docx2txt
from flask_cors import CORS, cross_origin


# Load environment variables
load_dotenv()

app = Flask(__name__)
cors = CORS(app)
app.config['CORS_HEADERS'] = 'Content-Type'
app.config['CORS_ORIGINS'] = '*'


# Configure R2 client (S3 compatible)
r2 = boto3.client(
    's3',
    endpoint_url=os.getenv('R2_ENDPOINT_URL'),
    aws_access_key_id=os.getenv('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('R2_SECRET_ACCESS_KEY'),
    region_name='apac'
)

# Configure MongoDB client
mongo_client = MongoClient(os.getenv('MONGO_URI'))
db = mongo_client[os.getenv('MONGO_DB_NAME')]
files_collection = db['files']
folders_collection = db['folders']
users_collection = db['users']
collaborations_collection = db['collaborations']
devices_collection = db['devices']

BUCKET_NAME = os.getenv('R2_BUCKET_NAME')
LIMIT_BY_EXT = False
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'docx'}

# Auth0 configuration
AUTH0_DOMAIN = os.getenv('AUTH0_DOMAIN')
AUTH0_CLIENT_ID = os.getenv('AUTH0_CLIENT_ID')
AUTH0_CLIENT_SECRET = os.getenv('AUTH0_CLIENT_SECRET')
AUTH0_AUDIENCE = os.getenv('AUTH0_AUDIENCE')

def get_auth0_token():
    get_token = GetToken(AUTH0_DOMAIN)
    token = get_token.client_credentials(AUTH0_CLIENT_ID,
                                         AUTH0_CLIENT_SECRET,
                                         AUTH0_AUDIENCE)
    return token['access_token']

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', None)
        if not token:
            return jsonify({"error": "Authorization header is expected"}), 401

        parts = token.split()

        if parts[0].lower() != "bearer":
            return jsonify({"error": "Authorization header must start with Bearer"}), 401
        elif len(parts) == 1:
            return jsonify({"error": "Token not found"}), 401
        elif len(parts) > 2:
            return jsonify({"error": "Authorization header must be Bearer token"}), 401

        token = parts[1]
        
        auth0_management = Auth0(AUTH0_DOMAIN, get_auth0_token())
        try:
            user_info = auth0_management.users.get(token)
            request.current_user = user_info
        except Exception:
            return jsonify({"error": "Invalid token"}), 401

        return f(*args, **kwargs)
    return decorated

def allowed_file(filename):
    if LIMIT_BY_EXT:
        return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
    else:
        return True
#    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_file(file):
    file_extension = file.filename.rsplit('.', 1)[1].lower()
    if file_extension == 'pdf':
        pdf_reader = PyPDF2.PdfReader(file)
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text()
    elif file_extension in ['png', 'jpg', 'jpeg']:
        image = Image.open(file)
        text = pytesseract.image_to_string(image)
    elif file_extension == 'docx':
        text = docx2txt.process(file)
    elif file_extension == 'txt':
        text = file.read().decode('utf-8')
    else:
        text = ""
    return text
@app.route('/',methods=['GET'])
def index():
    return jsonify({"status":"ok"}), 200

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        file_id = str(uuid.uuid4())
        folder_id = request.form.get('folder_id', None)
        
        # Extract text content from the file
        file_content = "test"
        
        # Reset file pointer to the beginning
        file.seek(0)
        
        r2.upload_fileobj(file, BUCKET_NAME, file_id)
        
        file_metadata = {
            "file_id": file_id,
            "filename": filename,
            "user_id": request.args.get('user_id'),
            "access_level": "private",
            "owner_id": request.args.get('user_id'),
            "shared_with": [],
            "shared_by": [],
            "shared_date": [],
            "upload_date": datetime.datetime.utcnow(),
            "is_deleted": False,
            "tags": [],
            "bookmarked": False,
            "favorited": False,
            "liked": False,
            "folder_id": folder_id,
            "content": file_content  # Store extracted text content
        }
        files_collection.insert_one(file_metadata)
        print(file_metadata)
        return jsonify({"message": "File uploaded successfully", "file_id": file_id}), 200
    return jsonify({"error": "File type not allowed"}), 400

@app.route('/download/<file_id>', methods=['GET'])
def download_file(file_id):
    file = files_collection.find_one({"file_id": file_id, "user_id": request.args.get('user_id')})
    if not file:
        return jsonify({"error": "File not found"}), 404
    
    try:
        file_obj = r2.get_object(Bucket=BUCKET_NAME, Key=file_id)
        return send_file(
            BytesIO(file_obj['Body'].read()),
            download_name=file['filename'],
            as_attachment=True
        )
    except ClientError:
        return jsonify({"error": "Error downloading file"}), 500

@app.route('/delete/<file_id>', methods=['DELETE'])
def delete_file(file_id):
    result = files_collection.update_one(
        {"file_id": file_id,
          "user_id": request.args.get('user_id')
          },
        {"$set": {"is_deleted": True}}
    )
    if result.modified_count:
        return jsonify({"message": "File moved to trash"}), 200
    return jsonify({"error": "File not found"}), 404

@app.route('/rename/<file_id>', methods=['PUT'])
def rename_file(file_id):
    new_filename = request.json.get('new_filename')
    if not new_filename:
        return jsonify({"error": "New filename is required"}), 400
    
    result = files_collection.update_one(
        {"file_id": file_id, 
         "user_id": request.json.get('user_id')
         },
        {"$set": {"filename": new_filename}}
    )
    if result.modified_count:
        return jsonify({"message": "File renamed successfully"}), 200
    return jsonify({"error": "File not found"}), 404

@app.route('/move/<file_id>', methods=['PUT'])
def move_file(file_id):
    new_folder_id = request.json.get('new_folder_id')
    if new_folder_id is None:
        return jsonify({"error": "New folder ID is required"}), 400
    
    # Check if the new folder exists
    if new_folder_id != "root":

        folder = folders_collection.find_one({"folder_id": new_folder_id,
                                               "user_id": request.json.get('user_id')
                                               })
        if not folder:
            folders_collection.insert_one({"folder_id": new_folder_id,
                                               "user_id": request.json.get('user_id')
                                               })


    result = files_collection.update_one(
        {"file_id": file_id, 
         "user_id": request.json.get('user_id')
         },
        {"$set": {"folder_id": new_folder_id}}
    )
    if result.modified_count:
        return jsonify({"message": "File moved successfully"}), 200
    return jsonify({"error": "File not found"}), 404

@app.route('/details/<file_id>', methods=['GET'])
def get_file_details(file_id):
    file = files_collection.find_one({"file_id": file_id, 
                                      "user_id": request.args.get('user_id')
                                      })
    if not file:
        return jsonify({"error": "File not found"}), 404
    
    return jsonify({
        "file_id": file['file_id'],
        "filename": file['filename'],
        "upload_date": file['upload_date'],
        # "author": request.current_user['name'],
        "tags": file.get('tags', []),
        "bookmarked": file.get('bookmarked', False),
        "favorited": file.get('favorited', False),
        "liked": file.get('liked', False),
        "folder_id": file.get('folder_id', 'root')
    }), 200

@app.route('/permanent_delete/<file_id>', methods=['DELETE'])
def permanent_delete_file(file_id):
    file = files_collection.find_one_and_delete({"file_id": file_id, 
                                                 "user_id": request.args.get('user_id')
                                                 })
    if not file:
        return jsonify({"error": "File not found"}), 404
    
    try:
        r2.delete_object(Bucket=BUCKET_NAME, Key=file_id)
        return jsonify({"message": "File permanently deleted"}), 200
    except ClientError:
        return jsonify({"error": "Error deleting file from R2"}), 500

@app.route('/tag/<file_id>', methods=['POST'])
def tag_file(file_id):
    tags = request.json.get('tags', [])
    result = files_collection.update_one(
        {"file_id": file_id, "user_id": request.args.get('user_id')},
        {"$addToSet": {"tags": {"$each": tags}}}
    )
    if result.modified_count:
        return jsonify({"message": "Tags added successfully"}), 200
    return jsonify({"error": "File not found"}), 404

@app.route('/bookmark/<file_id>', methods=['POST'])
def bookmark_file(file_id):
    result = files_collection.update_one(
        {"file_id": file_id, "user_id": request.args.get('user_id')},
        {"$set": {"bookmarked": True}}
    )
    if result.modified_count:
        return jsonify({"message": "File bookmarked"}), 200
    return jsonify({"error": "File not found"}), 404

@app.route('/favorite/<file_id>', methods=['POST'])
def favorite_file(file_id):
    result = files_collection.update_one(
        {"file_id": file_id, "user_id": request.args.get('user_id')},
        {"$set": {"favorited": True}}
    )
    if result.modified_count:
        return jsonify({"message": "File favorited"}), 200
    return jsonify({"error": "File not found"}), 404

@app.route('/like/<file_id>', methods=['POST'])
def like_file(file_id):
    result = files_collection.update_one(
        {"file_id": file_id, "user_id": request.args.get('user_id')},
        {"$set": {"liked": True}}
    )
    if result.modified_count:
        return jsonify({"message": "File liked"}), 200
    return jsonify({"error": "File not found"}), 404

@app.route('/share/<file_id>', methods=['POST'])
def share_file(file_id):
    email = request.json.get('email')
    if not email:
        return jsonify({"error": "Email is required"}), 400
    
    file = files_collection.find_one({"file_id": file_id, "user_id": request.args.get('user_id')})
    if not file:
        return jsonify({"error": "File not found"}), 404
    
    collaboration = {
        "file_id": file_id,
        "owner_id": request.args.get('user_id'),
        "shared_with": email,
        "shared_date": datetime.datetime.utcnow()
    }
    collaborations_collection.insert_one(collaboration)
    
    # Here you would typically send an email to the recipient
    # For this example, we'll just return a success message
    return jsonify({"message": f"File shared with {email}"}), 200

@app.route('/files', methods=['GET'])
def list_files():
    folder_id = request.args.get('folder_id', 'root')
    files = files_collection.find({
        "user_id": request.args.get('user_id'),
        "is_deleted": False,
        "folder_id": folder_id
    })
    return jsonify([{
        "file_id": file['file_id'],
        "filename": file['filename'],
        "upload_date": file['upload_date']
    } for file in files]), 200

@app.route('/search', methods=['GET'])
def search_files():
    query = request.args.get('q', '')
    files = files_collection.find({
        "user_id": request.args.get('user_id'),
        "is_deleted": False,
        "$or": [
            {"filename": {"$regex": query, "$options": "i"}},
            {"tags": {"$in": [query]}},
            {"content": {"$regex": query, "$options": "i"}}  # Search in extracted content
        ]
    })
    return jsonify([{
        "file_id": file['file_id'],
        "filename": file['filename'],
        "upload_date": file['upload_date']
    } for file in files]), 200

@app.route('/sync', methods=['POST'])
def sync_files():
    group = request.json.get('group')
    if not group:
        return jsonify({"error": "Group is required"}), 400
    
    # This is a simplified sync process. In a real-world scenario,
    # you'd need to handle conflicts and implement a more robust syncing mechanism.
    files = files_collection.find({"user_id": request.args.get('user_id'), "is_deleted": False})
    for file in files:
        collaboration = {
            "file_id": file['file_id'],
            "owner_id": request.args.get('user_id'),
            "shared_with": group,
            "shared_date": datetime.datetime.utcnow()
        }
        collaborations_collection.insert_one(collaboration)
    
    return jsonify({"message": f"Files synced with {group}"}), 200

@app.route('/devices', methods=['GET'])
# @requires_auth
def list_devices():
    devices = devices_collection.find({
        "user_id": request.args.get('user_id')
        })
    device_list = []
    for device in devices:
        device_list.append({
            "device_id": device['device_id'],
            "device_name": device['device_name'],
            "device_type": device['device_type']
        })
    print(devices)
    return jsonify(device_list), 200

@app.route('/devices', methods=['POST'])
# @requires_auth
def add_device():
    print(request.json.get('user_id'))
    device_data = request.json
    if not device_data or 'device_name' not in device_data or 'device_type' not in device_data:
        return jsonify({"error": "Device name and type are required"}), 400
    new_device = {
        "device_id": str(uuid.uuid4()),
        "user_id": device_data['user_id'],
        "device_name": device_data['device_name'],
        "device_type": device_data['device_type'],
        "added_date": datetime.datetime.utcnow()
    }
    print(new_device)
    result = devices_collection.insert_one(new_device)
    
    if result.inserted_id:
        return jsonify({
            "message": "Device added successfully",
            "device_id": new_device['device_id']
        }), 201
    else:
        return jsonify({"error": "Failed to add device"}), 500

@app.route('/feedback', methods=['POST'])
@cross_origin()
def feedback():
    feedback_data = request.data
    print(feedback_data)
    #feedback_data['date'] = datetime.datetime.utcnow()
    result = db['feedback'].insert_one({"feedback":feedback_data})
    #response.headers.add("Access-Control-Allow-Origin", "*")
    if feedback_data:    
    	return jsonify({"message": "Feedback submitted successfully"}), 201
    else:
        return jsonify({"error": "Failed to submit feedback"}), 500

if __name__ == '__main__':
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1000 * 1000 * 1000
    app.run(debug=True, host='0.0.0.0', port=5000)
    
