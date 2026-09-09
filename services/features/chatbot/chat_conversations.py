"""Bounded conversation memory, isolated by authenticated account."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4


class ConversationNotFound(Exception):
    pass


class ConversationConflict(Exception):
    pass


class ConversationStore:
    def __init__(self, collection, user):
        self.collection = collection
        self.user = user

    def load(self, conversation_id):
        if not conversation_id:
            return None
        doc = self.collection.find_one({"_id": conversation_id, "owner_user_id": self.user.user_id,
                                        "expires_at": {"$gt": datetime.now(timezone.utc)}},
                                       {"messages": 1, "revision": 1}, max_time_ms=3000)
        if doc is None:
            raise ConversationNotFound()
        return doc

    def save(self, conversation_id, previous, question, answer, initial_history=None):
        now = datetime.now(timezone.utc)
        messages = [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
        if previous is None:
            messages = list(initial_history or [])[-10:] + messages
            conversation_id = uuid4().hex
            self.collection.insert_one({"_id": conversation_id, "owner_user_id": self.user.user_id,
                                        "messages": messages, "revision": 1, "created_at": now,
                                        "updated_at": now, "expires_at": now + timedelta(days=30)})
        else:
            result = self.collection.update_one(
                {"_id": conversation_id, "owner_user_id": self.user.user_id,
                 "revision": previous["revision"]},
                {"$push": {"messages": {"$each": messages, "$slice": -12}},
                 "$inc": {"revision": 1}, "$set": {"updated_at": now, "expires_at": now + timedelta(days=30)}})
            if result.matched_count != 1:
                raise ConversationConflict()
        return conversation_id
