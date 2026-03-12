# Chatbot service: load NLP model and process messages.
# Pure logic called from chatbot routes.

from pymongo.collection import Collection
import time
import joblib
import os

from core.config import MODEL_DIR
import random
import re

# --------------------------------------------------
# LOAD NLP MODEL (lazy)
# --------------------------------------------------

MODELS_DIR = MODEL_DIR
_vectorizer = None
_nlp_model = None
_models_loaded = False


def _load_models():
    global _vectorizer, _nlp_model, _models_loaded
    if _models_loaded:
        return
    _vectorizer = joblib.load(os.path.join(MODELS_DIR, "vectorizer.pkl"))
    _nlp_model = joblib.load(os.path.join(MODELS_DIR, "ml_model.pkl"))
    _models_loaded = True
    print("NLP model loaded successfully")


# --------------------------------------------------
# NLP PREDICTION
# --------------------------------------------------

def predict_nlp(text: str):
    _load_models()
    X = _vectorizer.transform([text])
    label = _nlp_model.predict(X)[0]
    intent, entity, response_type = label.split("|")
    return intent, entity, response_type


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def normalize_entity(entity: str, detected_keys) -> str:
    if not entity:
        return ""
    raw = entity.strip().lower()
    if raw in detected_keys:
        return raw
    norm = re.sub(r"[\s\-]+", "_", raw)
    if norm in detected_keys:
        return norm
    for key in detected_keys:
        key_norm = re.sub(r"[\s\-]+", "_", key.lower())
        if key_norm == norm:
            return key
    return norm


def extract_tour_from_message(message: str, tours_collection: Collection):
    message = message.lower()
    for tour in tours_collection.find({}):
        name = (tour.get("name") or "").lower()
        if name and name in message:
            return tour
    return None


def get_latest_tour(tours_collection: Collection):
    return tours_collection.find_one(sort=[("created_at", -1)])


def build_object_counts(site_objects):
    counts = {}
    for obj in site_objects:
        obj_type = obj.get("type")
        if not obj_type:
            continue
        counts[obj_type] = counts.get(obj_type, 0) + 1
    return counts


def collect_comments(tour: dict):
    top = tour.get("comments")
    if isinstance(top, list) and top:
        return top

    all_comments = []
    for node in (tour.get("nodes") or []):
        node_comments = node.get("comments")
        if isinstance(node_comments, list) and node_comments:
            all_comments.extend(node_comments)

    return all_comments


def get_progress_percentage(progress: dict) -> float:
    val = progress.get("percentage")
    if val is None:
        val = (progress.get("summary") or {}).get("percentage")
    if val is None:
        return 0.0
    try:
        return round(float(val), 2)
    except Exception:
        return 0.0


def get_coverage_percentage(progress: dict, tour: dict) -> float:
    # Prefer explicit percent fields (0-100)
    val = (
        progress.get("coverage_percentage")
        or progress.get("coverage_percent")
        or progress.get("coverage")
        or (tour.get("coverage", {}) or {}).get("coverage_percentage")
        or (tour.get("coverage", {}) or {}).get("coverage_percent")
    )

    # If nothing found, fall back to coverage_ratio (0-1)
    if val is None:
        ratio = progress.get("coverage_ratio")
        if ratio is None:
            ratio = (tour.get("coverage", {}) or {}).get("coverage_ratio")
        if ratio is None:
            summary = progress.get("summary") or {}
            planned = summary.get("planned")
            covered = summary.get("covered")
            if planned:
                try:
                    return round((float(covered) / float(planned)) * 100, 2)
                except Exception:
                    return 0.0
            return 0.0
        try:
            return round(float(ratio) * 100, 2)
        except Exception:
            return 0.0

    # val might be "7", 7, 0.07, etc.
    try:
        val = float(val)
    except Exception:
        return 0.0

    # If it looks like 0-1, convert to %
    if 0 <= val <= 1:
        val = val * 100

    return round(val, 2)


# --------------------------------------------------
# RESPONSE TEMPLATES
# --------------------------------------------------

TEMPLATES = {
    # GET_TOUR_INFO|none|TOUR_INFO
    "TOUR_INFO": [
        "There are {count} tours uploaded: {tour_list}.",
        "I found {count} tours: {tour_list}.",
        "Total uploaded tours: {count}. Tours: {tour_list}."
    ],

    # GET_TOUR_DETAILS|none|TOUR_DETAILS
    # (You must prepare these keys in data: tour_name, percentage, coverage_percentage, node_count, issue_count, object_list)
    "TOUR_DETAILS": [
        (
        "Tour '{tour_name}' summary:\n"
        "- Progress: {percentage}%\n"
        "- Coverage: {coverage_percentage}%\n"
        "- Nodes (panoramas): {node_count}\n"
        "- Comments: {issue_count}\n"
        "- Objects detected: {object_list}"
        ),
        (
        "Details for '{tour_name}': Progress {percentage}%, Coverage {coverage_percentage}%, "
        "{node_count} nodes, {issue_count} comments. Objects: {object_list}."
        )
    ],

    # GET_PROGRESS|none|PROGRESS_SUMMARY
    "PROGRESS_SUMMARY": [
        "The tour '{tour_name}' is {percentage}% complete.",
        "{percentage}% of the construction work has been completed in '{tour_name}'.",
        "Current progress for '{tour_name}': {percentage}%."
    ],

    # GET_COVERAGE|none|COVERAGE_SUMMARY
    "COVERAGE_SUMMARY": [
        "Coverage for '{tour_name}' is {coverage_percentage}%.",
        "In '{tour_name}', {coverage_percentage}% of the site is covered.",
        "Current coverage in '{tour_name}': {coverage_percentage}%."
    ],

    # GET_DURATION|none|DURATION_SUMMARY
    # (You must prepare: tour_name, duration)
    "DURATION_SUMMARY": [
        "Tour duration for '{tour_name}' is {duration}.",
        "The tour '{tour_name}' took {duration}.",
        "Total time for '{tour_name}': {duration}."
    ],

    # GET_NODES|none|NODE_COUNT
    # (You must prepare: tour_name, count)
    "NODE_COUNT": [
        "There are {count} panoramas (nodes) in '{tour_name}'.",
        "'{tour_name}' has {count} capture points (nodes).",
        "Total nodes in '{tour_name}': {count}."
    ],

    # GET_OBJECTS|<entity>|OBJECT_COUNT
    "OBJECT_COUNT": [
        "There are {count} {object} detected in '{tour_name}'.",
        "{count} {object} were identified during the tour '{tour_name}'.",
        "Detected {count} {object} in '{tour_name}'."
    ],

    # GET_OBJECTS|all|OBJECT_LIST
    # (You must prepare: tour_name, object_list)
    "OBJECT_LIST": [
        "Objects detected in '{tour_name}': {object_list}.",
        "I found these object types in '{tour_name}': {object_list}.",
        "Detected assets in '{tour_name}': {object_list}."
    ],

    # GET_ISSUES|none|ISSUE_COUNT
    "ISSUE_COUNT": [
        "There are {count} reported comments in '{tour_name}'.",
        "Total comments in '{tour_name}': {count}.",
        "'{tour_name}' currently has {count} comments."
    ],

    # GET_ISSUES|none|ISSUE_LIST
    # (You must prepare: tour_name, bug_list)
    "ISSUE_LIST": [
        "Comments in '{tour_name}':\n{comment_list}",
        "Reported comments in '{tour_name}':\n{comment_list}"
    ],

    # GET_ISSUES|none|ISSUE_DETAILS
    # (You must prepare: title, department, reported_by, resolved_by, status)
    "ISSUE_DETAILS": [
        (
            "Issue: {title}\n"
            "Department: {department}\n"
            "Reported by: {reported_by}\n"
            "Resolved by: {resolved_by}\n"
            "Status: {status}"
        )
    ],

    "FALLBACK": [
        "I can help with tours, tour details, progress, coverage, duration, nodes, objects, and comments."
    ]
}


def render_response(response_type: str, data: dict):
    templates = TEMPLATES.get(response_type, TEMPLATES["FALLBACK"])
    return random.choice(templates).format(**data)


# --------------------------------------------------
# MAIN CHAT HANDLER
# --------------------------------------------------

def process_chat_message(message: str, tours_collection: Collection) -> dict:
    message = (message or "").strip()

    # ===============================
    # 1 LIST TOURS
    # ===============================
    if message == "__list_tours__":
        tours = list(tours_collection.find({}, {"tour_id": 1, "name": 1}))
        return {
            "tours": [
                {"tour_id": t.get("tour_id"), "name": t.get("name", "Unnamed Tour")}
                for t in tours
            ]
        }

    # ===============================
    # 2 TOUR DETAILS (MENU CLICK)
    # ===============================

    # ===============================
    # 3 NORMAL CHAT
    # ===============================
    intent, entity, response_type = predict_nlp(message)

    # TOUR_INFO must be handled BEFORE selecting a single tour
    # because it needs "count" + "tour_list", and doesn't require picking the latest tour.
    if response_type == "TOUR_INFO":
        tours = list(tours_collection.find({}, {"name": 1}))
        names = [(t.get("name") or "Unnamed Tour") for t in tours]

        data = {
            "count": len(names),
            "tour_list": ", ".join(names) if names else "none",
        }

        answer = render_response("TOUR_INFO", data)
        return {
            "answer": answer,
            "timestamp": int(time.time() * 1000),
        }

    tour = extract_tour_from_message(message, tours_collection)
    if not tour:
        tour = get_latest_tour(tours_collection)

    if not tour:
        return {"answer": "No tour data available."}

    progress = tour.get("progress", {})
    comments = collect_comments(tour)

    detected = tour.get("object_counts")
    if not detected:
        detected = build_object_counts(tour.get("site_objects") or tour.get("detections") or [])

    data = {"tour_name": tour.get("name", "Unnamed Tour")}

    if response_type == "TOUR_DETAILS":
        data["percentage"] = get_progress_percentage(progress)
        data["coverage_percentage"] = get_coverage_percentage(progress, tour)
        data["node_count"] = len(tour.get("nodes") or [])
        data["issue_count"] = len(comments)
        if detected:
            data["object_list"] = ", ".join(sorted(detected.keys()))
        else:
            data["object_list"] = "none"

    elif response_type == "PROGRESS_SUMMARY":
        data["percentage"] = get_progress_percentage(progress)

    elif response_type == "OBJECT_COUNT":
        entity_key = normalize_entity(entity, detected.keys())
        data["object"] = entity_key.replace("_", " ")
        data["count"] = detected.get(entity_key, 0)

    elif response_type == "OBJECT_LIST":
        if detected:
            data["object_list"] = ", ".join(sorted(detected.keys()))
        else:
            data["object_list"] = "none"

    elif response_type in ("COVERAGE_DETAILS", "COVERAGE_SUMMARY"):
        data["captured_length_m"] = progress.get("captured_length_m", 0)
        data["total_length_m"] = progress.get("total_length_m", 0)
        data["coverage_ratio"] = progress.get("coverage_ratio", 0)
        data["coverage_percentage"] = get_coverage_percentage(progress, tour)

    elif response_type == "ISSUE_COUNT":
        data["count"] = len(comments)

    elif response_type == "ISSUE_LIST":
        if not comments:
            return {
                "answer": f"No comments found in '{data['tour_name']}'.",
                "timestamp": int(time.time() * 1000),
            }

        lines = []
        for i, c in enumerate(comments, start=1):
            title = c.get("title") or "Untitled"
            dept = c.get("department") or "N/A"
            lines.append(f"{i}. {title} ({dept})")

        data["comment_list"] = "\n".join(lines)

    elif response_type == "ISSUE_DETAILS":
        if not comments:
            return {
                "answer": f"No comments found in '{data['tour_name']}'.",
                "timestamp": int(time.time() * 1000),
            }

        bug = comments[-1]  # latest comment
        data.update({
            "title": bug.get("title") or "Untitled",
            "department": bug.get("department") or "N/A",
            "reported_by": bug.get("created_by") or "N/A",
            "resolved_by": bug.get("resolved_by") or "N/A",
            "status": bug.get("status") or "Open",
        })

    answer = render_response(response_type, data)

    return {
        "answer": answer,
        "timestamp": int(time.time() * 1000),
    }
