import time
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Any, Optional

class Timer:
    def __init__(self):
        self.stages: List[Dict[str, Any]] = []
        self.start_time: float = time.perf_counter()
        self.end_time: Optional[float] = None

    @contextmanager
    def track(self, stage_name: str, is_llm: Optional[bool] = None):
        """Context manager to measure wall-clock duration for a pipeline stage."""
        t0 = time.perf_counter()
        try:
            yield
        finally:
            t1 = time.perf_counter()
            duration = t1 - t0
            
            # Auto-detect LLM/AI stage if not explicitly flagged
            if is_llm is None:
                name_lower = stage_name.lower()
                is_llm_stage = any(k in name_lower for k in ["llm", "ai", "reasoning", "gemini", "bundle resolution"])
            else:
                is_llm_stage = bool(is_llm)

            self.stages.append({
                "stage": stage_name,
                "duration_sec": round(duration, 4),
                "type": "llm" if is_llm_stage else "deterministic"
            })

    def summary(self, total_records: int = 0) -> Dict[str, Any]:
        """Compute performance metrics summary across all tracked stages."""
        total_time = sum(s["duration_sec"] for s in self.stages)
        if total_time <= 0:
            total_time = 0.0001  # Prevent div by zero

        det_time = sum(s["duration_sec"] for s in self.stages if s["type"] == "deterministic")
        llm_time = sum(s["duration_sec"] for s in self.stages if s["type"] == "llm")

        records_per_sec = round(total_records / total_time, 2) if total_time > 0 and total_records > 0 else 0.0

        stages_with_pct = []
        for s in self.stages:
            stages_with_pct.append({
                "stage": s["stage"],
                "duration_sec": s["duration_sec"],
                "type": s["type"],
                "pct_of_total": round((s["duration_sec"] / total_time) * 100, 1)
            })

        return {
            "total_duration_sec": round(total_time, 4),
            "total_records": total_records,
            "throughput_records_per_sec": records_per_sec,
            "deterministic_time_sec": round(det_time, 4),
            "deterministic_pct": round((det_time / total_time) * 100, 1),
            "llm_time_sec": round(llm_time, 4),
            "llm_pct": round((llm_time / total_time) * 100, 1),
            "stages": stages_with_pct
        }

    def save(self, filepath: str | Path, total_records: int = 0) -> Dict[str, Any]:
        """Save timing metrics to a JSON file."""
        target_path = Path(filepath)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        data = self.summary(total_records=total_records)
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f" Saved pipeline timing to {target_path}")
        return data

    @staticmethod
    def load(filepath: str | Path) -> Optional[Dict[str, Any]]:
        """Load timing metrics from a JSON file if present."""
        target_path = Path(filepath)
        if not target_path.exists():
            return None
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

if __name__ == "__main__":
    # Standalone demo & test
    t = Timer()
    with t.track("Tier 0: LLM Extraction"):
        time.sleep(0.4)
    with t.track("Tier 1: Exact Match"):
        time.sleep(0.02)
    with t.track("Tier 2: Fuzzy Match"):
        time.sleep(0.03)
    with t.track("Tier 3: Math-Only & Identity Veto"):
        time.sleep(0.02)
    with t.track("Tier 4: AI Bundle Resolution"):
        time.sleep(0.5)
    
    summary = t.summary(total_records=100)
    print("Test Summary:", json.dumps(summary, indent=2))
