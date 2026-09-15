"""Preview or backfill one tour's workforce image records, retaining a BSON backup.

Run from the backend directory: python -m scripts.migrate_workforce_images TOUR_ID
Add --apply only after reviewing the preview. Requires the normal backend DB config.
"""
import argparse
import hashlib
import json
from pathlib import Path

from bson import json_util
from core.database import raw_safety_analysis_jobs_collection as jobs
from core.database import raw_safety_records_collection as records
from core.database import raw_tours_collection as tours
from services.safety.safety_service import store_workforce_image_observations, workforce_image_observations


def migrate(tour_id, apply=False, backup_root=Path('/data/workforce-image-backups')):
    tour = tours.find_one({'tour_id': tour_id})
    job = jobs.find_one({'tour_id': tour_id}, sort=[('requested_at', -1)])
    if not tour or not job:
        raise RuntimeError('Tour and an existing safety analysis job are required')
    query = {'project_id': job['project_id'], 'tour_id': tour_id, 'record_type': 'workforce_observation'}
    planned = workforce_image_observations(job, tour)
    if not planned:
        raise RuntimeError('No counted images with valid node IDs; no changes made')
    print(json.dumps({'tour': tour.get('name') or tour.get('tour_name'), 'tour_id': tour_id,
                      'counted_images': len(planned), 'images_with_workers': sum(r['observed_workers'] > 0 for r in planned),
                      'images': [{'point': r['point_index'], 'workers': r['observed_workers']} for r in planned]}))
    if not apply:
        return
    backup = backup_root / hashlib.sha256(tour_id.encode()).hexdigest()[:20]
    backup.mkdir(parents=True, exist_ok=True)
    backup_file = backup / 'before.bson.json'
    if not backup_file.exists():
        backup_file.write_text(json_util.dumps({'tour': tour, 'job': job, 'records': list(records.find(query))}), encoding='utf-8')
    ids = store_workforce_image_observations(job, tour)
    assert records.count_documents({**query, 'record_id': {'$in': ids}}) == len(ids)
    assert json_util.dumps(tours.find_one({'tour_id': tour_id})) == json_util.dumps(tour), 'Tour unexpectedly changed'
    print(json.dumps({'saved_images': len(ids), 'backup': str(backup_file), 'tour_unchanged': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tour_id')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    migrate(args.tour_id, args.apply)
