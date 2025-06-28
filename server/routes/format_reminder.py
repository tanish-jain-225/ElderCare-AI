from flask import Blueprint, request, jsonify, Response
try:
    from together import Together
except ImportError:
    # Fallback if together import fails
    class Together:
        def __init__(self, api_key):
            self.api_key = api_key
        def chat(self):
            return self
        def completions(self):
            return self
        def create(self, **kwargs):
            # Mock response
            class MockResponse:
                choices = [type('obj', (object,), {'message': type('obj', (object,), {'content': '{"error": "Together API not available"}'})()})]
            return MockResponse()
import requests
import re
import json as pyjson
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from bson import ObjectId
from datetime import datetime
import json
from dateutil.parser import parse as parse_datetime
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


format_reminder_bp = Blueprint('format_reminder', __name__)
# Custom JSON encoder to handle MongoDB ObjectId
class MongoJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        elif isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)
        
# Helper function to convert MongoDB documents to JSON-friendly format
def convert_to_json_friendly(document):
    if document is None:
        return None
    if isinstance(document, list):
        return [convert_to_json_friendly(item) for item in document]
    
    result = {}
    for key, value in document.items():
        if key == '_id' and isinstance(value, ObjectId):
            result['id'] = str(value)
        elif isinstance(value, ObjectId):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif isinstance(value, dict):
            result[key] = convert_to_json_friendly(value)
        elif isinstance(value, list):
            result[key] = [convert_to_json_friendly(item) if isinstance(item, dict) else item for item in value]
        else:
            result[key] = value
    return result

# Initialize MongoDB connection settings
mongo_url = os.environ.get('MONGO_URI')
db_name = os.environ.get('DB_NAME')
reminders_collection_name = os.environ.get('REMINDERS_COLLECTION')

# Initialize Together AI client
together_api_key = os.environ.get('TOGETHER_API_KEY')
client = Together(api_key=together_api_key)

# Initialize MongoDB client
if mongo_url:
    mongo_client = MongoClient(mongo_url)
    db = mongo_client[db_name] 
    reminders_collection = db[reminders_collection_name]

def process_reminders(reminders_list, user_id):
    """Process multiple reminders and save them to MongoDB"""
    results = []
    errors = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    for reminder in reminders_list:
        try:
            # Use today's date if no date is provided
            date = reminder.get('date') or today_str
            # Use "New Reminder" as title if not provided
            title = reminder.get('title') or "New Reminder"
            time = reminder.get('time') or ""
            reminder_data = {"userId": user_id, "title": title, "date": date, "time": time}
            saved_reminder = save_to_mongodb(reminder_data)
            results.append(saved_reminder)
        except Exception as e:
            error_msg = f"Error processing reminder: {str(e)}"
            print(error_msg)
            errors.append(error_msg)
    if not results:
        return jsonify({"error": "No valid reminders found", "details": errors}), 400
    return jsonify({
        "success": True,
        "reminders": results,
        "count": len(results),
        "errors": errors if errors else None
    })

@format_reminder_bp.route('/format-reminder', methods=['POST', 'OPTIONS'])
def format_reminder():
    # Handle preflight OPTIONS request
    if request.method == 'OPTIONS':
        response = jsonify({'status': 'success'})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        response.headers.add('Access-Control-Allow-Methods', 'POST')
        return response
        
    print("POST /format-reminder endpoint called")
    print(f"Request data: {request.json}")
    
    # Important - ensure we have a JSON body with 'input' field and userId
    user_input = request.json.get('input', '')
    user_id = request.json.get('userId')
    # Ensure we have input to process
    if not user_input:
        return jsonify({"error": "No input provided. Please send JSON with 'input' field."}), 400
    if not user_id:
        return jsonify({"error": "No userId provided. Please send JSON with 'userId' field."}), 400
        
    # Instruct the LLM to format the input as a reminder with title, date, time
    response = client.chat.completions.create(
        model="deepseek-ai/DeepSeek-V3",
        messages=[
            {
                "role": "system",
                "content": "Format user input as one or more reminders. Extract title, date and time for each reminder. Always return a JSON array with each reminder having id, title, date and time fields. Date should be in YYYY-MM-DD format. If date is not mentioned set it has null and same for the title. Time should be in HH:MM format. If there are multiple reminders in the input, create multiple JSON objects in the array."
            },
            {
                "role": "user",
                "content": f'Parse this into reminders: {user_input}'
            }
        ]
    )
    
    content = response.choices[0].message.content
    print(f"LLM Response: {content}")
    
    # Try to extract an array first - handle markdown code blocks.
    try:
        # Look for JSON array in markdown code block or regular text
        array_match = re.search(r'```(?:json)?\s*(\[[\s\S]*?\])\s*```|(\[[\s\S]*?\])', content)
        if array_match:
            # Get the first matching group that's not None
            array_text = next(group for group in array_match.groups() if group is not None)
            reminders_array = pyjson.loads(array_text)
            if isinstance(reminders_array, list) and len(reminders_array) > 0:
                # Use today's date and default title if missing
                for r in reminders_array:
                    if not r.get('date'):
                        r['date'] = datetime.now().strftime("%Y-%m-%d")
                    if not r.get('title'):
                        r['title'] = "New Reminder"
                return process_reminders(reminders_array, user_id)
    except Exception as e:
        print(f"Error extracting array: {str(e)}")
    
    # If not an array, extract a single JSON object - handle markdown code blocks
    match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```|(\{[\s\S]*?\})', content)
    if match:
        try:
            # Get the first matching group that's not None
            json_text = next(group for group in match.groups() if group is not None)
            reminder_json = pyjson.loads(json_text)
            # Use today's date and default title if missing
            title = reminder_json.get('title') or "New Reminder"
            date = reminder_json.get('date') or datetime.now().strftime("%Y-%m-%d")
            time = reminder_json.get('time') or ""
            post_data = {"userId": user_id, "title": title, "date": date, "time": time}
            saved_reminder = save_to_mongodb(post_data)
            return jsonify({"success": True, "reminder": saved_reminder})
        except Exception as e:
            return jsonify({"error": "Failed to parse or post JSON", "details": str(e), "raw": content}), 400
    return jsonify({"error": "No JSON found in LLM response", "raw": content}), 400

def save_to_mongodb(reminder):
    """Save a reminder to MongoDB"""
    reminder_to_save = reminder.copy()
    now = datetime.now()
    reminder_to_save['created_at'] = now
    reminder_to_save['updated_at'] = now
    result = reminders_collection.insert_one(reminder_to_save)
    inserted_id = result.inserted_id
    print(f"Saved reminder to MongoDB with _id: {inserted_id}")
    json_safe_reminder = convert_to_json_friendly(reminder_to_save)
    json_safe_reminder['id'] = str(inserted_id)
    return json_safe_reminder

@format_reminder_bp.route('/reminders', methods=['GET'])
def get_reminders():
    user_id = request.args.get("userId")
    if not user_id:
        return jsonify({"error": "userId is required"}), 400
    try:
        cursor = reminders_collection.find({"userId": user_id})
        reminders_list = list(cursor)
        print(f"Found {len(reminders_list)} reminders for user {user_id}")
        
        # Convert ObjectId to string and ensure all fields are properly formatted
        formatted_reminders = []
        for reminder in reminders_list:
            formatted_reminder = {
                "id": str(reminder.get("_id", reminder.get("id", ""))),
                "title": reminder.get("title", ""),
                "date": reminder.get("date", ""),
                "time": reminder.get("time", ""),
                "userId": reminder.get("userId", ""),
                "created_at": reminder.get("created_at", datetime.now()).isoformat(),
                "updated_at": reminder.get("updated_at", datetime.now()).isoformat()
            }
            formatted_reminders.append(formatted_reminder)
        
        print(f"Formatted reminders: {formatted_reminders}")
        return jsonify({
            "success": True, 
            "reminders": formatted_reminders, 
            "count": len(formatted_reminders)
        })
    except Exception as e:
        print(f"Error in get_reminders: {str(e)}")
        return jsonify({"error": str(e)}), 500

@format_reminder_bp.route('/reminders/<reminder_id>', methods=['GET'])
def get_reminder_by_id(reminder_id):
    try:
        reminder = None
        if ObjectId.is_valid(reminder_id):
            reminder = reminders_collection.find_one({"_id": ObjectId(reminder_id)})
        if not reminder:
            return jsonify({"error": f"Reminder with ID {reminder_id} not found"}), 404
        reminder = convert_to_json_friendly(reminder)
        return jsonify({"success": True, "reminder": reminder})
    except Exception as e:
        print(f"Error in get_reminder_by_id: {str(e)}")
        return jsonify({"error": str(e)}), 500

@format_reminder_bp.route('/reminder-data', methods=['POST', 'OPTIONS'])
def save_reminder_data():
    if request.method == 'OPTIONS':
        response = jsonify({'status': 'success'})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        response.headers.add('Access-Control-Allow-Methods', 'POST')
        return response
    print("POST /reminder-data endpoint called")
    reminder_data = request.json
    if not reminder_data:
        return jsonify({"error": "No reminder data provided"}), 400
    try:
        print(f"Processing reminder data: {reminder_data}")
        if isinstance(reminder_data, list):
            results = []
            for reminder in reminder_data:
                saved = save_to_mongodb(reminder)
                if saved:
                    results.append(saved)
            response = jsonify({
                "success": True, 
                "reminders": results, 
                "count": len(results)
            })
        else:
            saved_reminder = save_to_mongodb(reminder_data)
            response = jsonify({"success": True, "reminder": saved_reminder})
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response
    except Exception as e:
        print(f"Error in save_reminder_data: {str(e)}")
        return jsonify({
            "error": f"Failed to save reminder data: {str(e)}"
        }), 500

@format_reminder_bp.route('/delete-reminder', methods=['POST'])
def delete_reminder():
    try:
        data = request.json
        reminder_id = data.get("id")
        user_id = data.get("userId")
        if not reminder_id or not user_id:
            return jsonify({"error": "Both id and userId are required"}), 400
        result = reminders_collection.delete_one({"_id": ObjectId(reminder_id), "userId": user_id})
        if result.deleted_count == 0:
            return jsonify({"error": f"Reminder with ID {reminder_id} and userId {user_id} not found"}), 404
        return jsonify({"success": True, "message": f"Reminder with ID {reminder_id} deleted"})
    except Exception as e:
        print(f"Error in delete_reminder: {str(e)}")
        return jsonify({"error": str(e)}), 500