"""Basic sanity test for lab_monitoring package entry points.
Ensures that running the package as a module exits with status 0
(the backup monitor may emit a warning status, which is considered success).
"""
import subprocess
import sys

def test_main_exit_code():
    # Run the package via python -m
    result = subprocess.run([
        sys.executable,
        "-m",
        "lab_monitoring"
    ], cwd="/root/LabDoctorM/projects/lab-monitoring", capture_output=True, text=True)
    # Мониторинг всегда завершается с exit 0 — результат передаётся через JSON
    assert result.returncode == 0, f"Unexpected exit code {result.returncode}\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}"
