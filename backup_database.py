from app import backup_database, verify_backup


if __name__ == "__main__":
    backup_path = backup_database()
    restored_feedback_count = verify_backup(backup_path)
    print(
        f"Backup verified: {backup_path} "
        f"({restored_feedback_count} feedback records restored in test)"
    )
