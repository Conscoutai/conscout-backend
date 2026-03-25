# Database module: Mongo client and collection handles.
# Shared by services and routes.

from __future__ import annotations

from pymongo import MongoClient

from core.auth_context import merge_owner_filter, stamp_owned_document
from core.config import MONGO_URI, DB_NAME


class ScopedCollection:
    def __init__(self, raw_collection):
        self._raw = raw_collection

    def find_one(self, filter=None, *args, **kwargs):
        scoped_filter = merge_owner_filter(filter)
        return self._raw.find_one(scoped_filter, *args, **kwargs)

    def find(self, filter=None, *args, **kwargs):
        scoped_filter = merge_owner_filter(filter)
        return self._raw.find(scoped_filter, *args, **kwargs)

    def update_one(self, filter, update, *args, **kwargs):
        scoped_filter = merge_owner_filter(filter)
        return self._raw.update_one(scoped_filter, update, *args, **kwargs)

    def update_many(self, filter, update, *args, **kwargs):
        scoped_filter = merge_owner_filter(filter)
        return self._raw.update_many(scoped_filter, update, *args, **kwargs)

    def delete_one(self, filter, *args, **kwargs):
        scoped_filter = merge_owner_filter(filter)
        return self._raw.delete_one(scoped_filter, *args, **kwargs)

    def delete_many(self, filter, *args, **kwargs):
        scoped_filter = merge_owner_filter(filter)
        return self._raw.delete_many(scoped_filter, *args, **kwargs)

    def insert_one(self, document, *args, **kwargs):
        return self._raw.insert_one(stamp_owned_document(document), *args, **kwargs)

    def aggregate(self, pipeline, *args, **kwargs):
        owner_match = merge_owner_filter(None)
        if owner_match:
            pipeline = [{"$match": owner_match}, *pipeline]
        return self._raw.aggregate(pipeline, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._raw, name)


client = MongoClient(MONGO_URI)
db = client[DB_NAME]

raw_floorplans_collection = db["sites"]
raw_tours_collection = db["tours"]
raw_work_schedules_collection = db["work_schedules"]
raw_users_collection = db["users"]

# Store site-related data in a single collection.
# This replaces the old floorplans collection name.
floorplans_collection = ScopedCollection(raw_floorplans_collection)
tours_collection = ScopedCollection(raw_tours_collection)
work_schedules_collection = ScopedCollection(raw_work_schedules_collection)
users_collection = raw_users_collection
