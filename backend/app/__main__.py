"""
Celery Application Entry Point

Run Celery worker: celery -A app.celery_app worker --loglevel=info
Run Celery beat: celery -A app.celery_app beat --loglevel=info
"""

from app.celery import celery_app

__all__ = ["celery_app"]
