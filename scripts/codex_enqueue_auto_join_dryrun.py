from app.core.scheduler.tasks import auto_join_groups_task


result = auto_join_groups_task.apply_async(
    kwargs={
        "max_accounts": 1,
        "keywords_per_account": 2,
        "max_groups_per_keyword": 2,
        "dry_run": True,
    },
    queue="automation",
)
print(result.id)
