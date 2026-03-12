# Database module: Mongo client and collection handles.
# Shared by services and routes.

from pymongo import MongoClient
from core.config import MONGO_URI, DB_NAME

client = MongoClient(MONGO_URI)
db = client[DB_NAME]

# Store site-related data in a single collection.
# This replaces the old floorplans collection name.
floorplans_collection = db["sites"]
tours_collection = db["tours"]
work_schedules_collection = db["work_schedules"]