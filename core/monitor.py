"""
AutoResearcher Experiment Monitor

The key innovation: ZERO LLM calls during experiment training.

While your model trains (hours/days), the monitor only does:
- Process alive check (kill -0 PID)
- Log file tail read
- GPU utilization check

This means running AutoResearcher 24/7 costs the same as running it
only during the THINK and REFLECT phases.
"""

import os
import time
import subprocess
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("autoresearcher.monitor")


class ExperimentMonitor:
    """Zero-LLM experiment monitoring.

    Design principle: During training, the agent is effectively "sleeping"
    at zero cost. It only wakes up (calls LLM) when training completes
    and results need analysis.
    """

    def __init__(self, poll_interval: int = 900, zero_llm: bool = True):
        self.poll_interval = poll_interval  # seconds between checks
        self.zero_llm = zero_llm
        self._active_experiments: dict[int, dict] = {}

    def launch_experiment(self, command: str, log_file: str, gpu: Optional[str] = None) -> dict:
        """Launch an experiment via nohup and track its PID.

        Args:
            command: The training command to run
            log_file: Path to redirect stdout/stderr
            gpu: CUDA_VISIBLE_DEVICES value

        Returns:
            dict with pid, log_file, start_time
        """
        env = os.environ.copy()
        if gpu is not None:
            env["CUDA_VISIBLE_DEVICES"] = str(gpu)

        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        with open(log_path, "w") as log_f:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                env=env,
                preexec_fn=os.setsid,  # New process group (survives parent death)
            )

        experiment = {
            "pid": process.pid,
            "log_file": str(log_path),
            "start_time": time.time(),
            "command": command,
            "status": "running",
        }
        self._active_experiments[process.pid] = experiment

        logger.info(f"Launched experiment: PID={process.pid}, cmd={command[:80]}...")
        return experiment

    def wait_for_completion(self, pid: int, log_file: str, notify: bool = True) -> dict:
        """Wait for experiment to complete. ZERO LLM calls during wait.

        This is the core cost-saving mechanism. Instead of asking the LLM
        "is training done?", we just check if the process is alive.
        """
        logger.info(f"Monitoring PID={pid}, polling every {self.poll_interval}s")

        while self._is_process_alive(pid):
            time.sleep(self.poll_interval)

            # Log current status (no LLM involved)
            gpu_info = self._get_gpu_status()
            log_tail = self._tail_file(log_file, lines=5)
            elapsed = time.time() - self._active_experiments.get(pid, {}).get("start_time", time.time())

            logger.info(
                f"PID={pid} alive | elapsed={elapsed/3600:.1f}h | "
                f"GPU={gpu_info.get('utilization', 'N/A')} | "
                f"last_log: {log_tail[-1] if log_tail else 'N/A'}"
            )

        # Experiment finished
        elapsed = time.time() - self._active_experiments.get(pid, {}).get("start_time", time.time())
        log_tail = self._tail_file(log_file, lines=50)

        if pid in self._active_experiments:
            self._active_experiments[pid]["status"] = "completed"

        result = {
            "pid": pid,
            "status": "completed",
            "elapsed_hours": elapsed / 3600,
            "log_tail": "\n".join(log_tail),
            "metrics": self._extract_metrics(log_tail),
        }

        logger.info(f"Experiment PID={pid} completed after {result['elapsed_hours']:.1f}h")

        if notify:
            self._notify_completion(result)

        return result

    def has_completed_experiments(self) -> bool:
        """Check if any tracked experiment has finished."""
        for pid, exp in list(self._active_experiments.items()):
            if exp["status"] == "running" and not self._is_process_alive(pid):
                exp["status"] = "completed"
                return True
        return False

    def _is_process_alive(self, pid: int) -> bool:
        """Check if process is still running (zero cost)."""
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _get_gpu_status(self) -> dict:
        """Get GPU utilization via nvidia-smi."""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                gpus = []
                for line in lines:
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 3:
                        gpus.append({
                            "utilization": f"{parts[0]}%",
                            "memory": f"{parts[1]}MB/{parts[2]}MB",
                        })
                return {"gpus": gpus, "utilization": gpus[0]["utilization"] if gpus else "N/A"}
        except Exception:
            pass
        return {"utilization": "N/A"}

    def _tail_file(self, filepath: str, lines: int = 50) -> list[str]:
        """Read last N lines of a file (zero cost)."""
        try:
            with open(filepath, "r") as f:
                all_lines = f.readlines()
                return [l.rstrip() for l in all_lines[-lines:]]
        except Exception:
            return []

    def _extract_metrics(self, log_lines: list[str]) -> dict:
        """Try to extract common metrics from training logs.

        Looks for patterns like:
        - loss: 0.123
        - accuracy: 95.2%
        - FGD: 0.582
        - epoch 100/200
        """
        import re
        metrics = {}
        for line in reversed(log_lines):
            # Common metric patterns
            for pattern, key in [
                (r"loss[:\s]+([0-9.]+)", "loss"),
                (r"acc(?:uracy)?[:\s]+([0-9.]+)", "accuracy"),
                (r"FGD[:\s]+([0-9.]+)", "FGD"),
                (r"FID[:\s]+([0-9.]+)", "FID"),
                (r"epoch[:\s]+(\d+)", "epoch"),
                (r"step[:\s]+(\d+)", "step"),
            ]:
                if key not in metrics:
                    match = re.search(pattern, line, re.IGNORECASE)
                    if match:
                        metrics[key] = match.group(1)
        return metrics

    def _notify_completion(self, result: dict):
        """Send notification when experiment completes."""
        logger.info(
            f"EXPERIMENT COMPLETE | PID={result['pid']} | "
            f"Time={result['elapsed_hours']:.1f}h | "
            f"Metrics={result.get('metrics', {})}"
        )
