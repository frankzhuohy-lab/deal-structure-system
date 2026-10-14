"""启动台账系统 Flask 服务并写入日志"""
import subprocess, sys, time, os

APP_DIR = r"C:\Users\frank\Desktop\皖信台账系统"
PYTHON = os.path.join(APP_DIR, ".venv", "Scripts", "python.exe")
APP = os.path.join(APP_DIR, "app.py")
LOG = os.path.join(APP_DIR, "startup.log")

with open(LOG, "w", encoding="utf-8") as f:
    f.write(f"Starting Flask at {time.ctime()}\n")
    f.write(f"Python: {PYTHON}\n")
    f.write(f"App: {APP}\n")
    
    try:
        proc = subprocess.Popen(
            [PYTHON, "-u", APP],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=APP_DIR
        )
        f.write(f"PID: {proc.pid}\n")
        f.flush()
        
        # Read output for 10 seconds
        start = time.time()
        while time.time() - start < 10:
            line = proc.stdout.readline()
            if line:
                f.write(line)
                f.flush()
                print(line, end="")
            if proc.poll() is not None:
                f.write(f"\nProcess exited with code {proc.returncode}\n")
                print(f"EXITED: {proc.returncode}")
                break
        
        if proc.poll() is None:
            f.write("\nStill running after 10s\n")
            print("RUNNING_OK")
            proc.terminate()
    except Exception as e:
        f.write(f"ERROR: {e}\n")
        print(f"ERROR: {e}")
