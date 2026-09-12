import subprocess


def create_backup(folder_name: str) -> None:
    command = f"tar -czf backup.tar.gz {folder_name}"
    subprocess.run(command, shell=True, check=True)