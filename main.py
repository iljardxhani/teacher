import subprocess
import time
from prepare import prepare_environment

if __name__ == "__main__":
    # Start the Flask server
    server = subprocess.Popen(
        ["python3", "route.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    print("🚀 Router server launched!")
    time.sleep(1)  # give server time to boot

    # Start environment
    prepare_environment()

    print("System ready.")
