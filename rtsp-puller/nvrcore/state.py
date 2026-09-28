# -*- coding: utf-8 -*-
"""断点状态：记录下一次要拉取的起始时间，支持中断后继续。"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

_FMT = "%Y-%m-%d %H:%M:%S"


@dataclass
class State:
    next_start: Optional[str] = None          # 下一次拉取的起始时间
    last_success_end: Optional[str] = None    # 最近一次成功拉取的结束时间
    last_run: Optional[str] = None
    total_segments: int = 0
    total_bytes: int = 0
    total_gap_segments: int = 0
    consecutive_errors: int = 0
    last_error: Optional[str] = None
    started_at: Optional[str] = None

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: str) -> "State":
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                allowed = set(cls.__dataclass_fields__)
                return cls(**{k: v for k, v in data.items() if k in allowed})
            except Exception:
                pass
        return cls()

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.last_run = datetime.now().strftime(_FMT)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    def get_next_start(self) -> Optional[datetime]:
        if not self.next_start:
            return None
        try:
            return datetime.strptime(self.next_start, _FMT)
        except ValueError:
            return None

    def set_next_start(self, dt: datetime) -> None:
        self.next_start = dt.strftime(_FMT)

    def describe(self) -> str:
        return ("下次拉取起点 : %s\n"
                "累计成功     : %d 个文件 / %.2f GB\n"
                "空档跳过     : %d 段\n"
                "连续失败     : %d 次\n"
                "最近一次运行 : %s"
                % (self.next_start or "(未初始化)",
                   self.total_segments, self.total_bytes / 1024 ** 3,
                   self.total_gap_segments, self.consecutive_errors,
                   self.last_run or "-"))


# --------------------------------------------------------------------------- #
#  索引文件（state/index.jsonl）
# --------------------------------------------------------------------------- #
def rewrite_index_paths(index_file: str, mapping: dict, dry_run: bool = False) -> int:
    """把索引里指向旧路径的记录同步成新路径，返回改动条数。

    索引是后续上传 Notion 的数据源，也是"这段录像在哪"的唯一台账。
    凡是移动或改名过归档文件的工具，都必须调用本函数同步，否则路径失效。
    mapping 的键用 os.path.normcase 规范化后的旧路径。
    """
    if not mapping or not os.path.isfile(index_file):
        return 0
    records: list = []
    changed = 0
    with open(index_file, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
            except ValueError:
                records.append(s)                     # 坏行原样保留
                continue
            p = rec.get("file")
            if p and os.path.normcase(p) in mapping:
                rec["file"] = mapping[os.path.normcase(p)]
                changed += 1
            records.append(json.dumps(rec, ensure_ascii=False))
    if changed and not dry_run:
        with open(index_file, "w", encoding="utf-8") as f:
            f.write("".join(r + "\n" for r in records))
    return changed
