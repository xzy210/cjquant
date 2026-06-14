"""策略基金池配置文件读写（回测与实盘共用）。"""

import json
import os
from typing import Any, Dict, List, Optional

POOLS_SUBDIR = "pools"


def fund_pool_path_for_strategy(strategy_name: str, strategies_dir: str) -> str:
    """策略文件名 → pools/<stem>.json 路径。"""
    stem = os.path.splitext(os.path.basename(strategy_name))[0]
    return os.path.join(strategies_dir, POOLS_SUBDIR, f"{stem}.json")


def fund_pool_path_for_script(script_path: str) -> str:
    strategy_dir = os.path.dirname(os.path.abspath(script_path))
    stem = os.path.splitext(os.path.basename(script_path))[0]
    return os.path.join(strategy_dir, POOLS_SUBDIR, f"{stem}.json")


def normalize_fund_pool(raw: dict) -> dict:
    """校验并规范化基金池结构。"""
    if not isinstance(raw, dict):
        raise ValueError("基金池配置必须是 JSON 对象")
    funds = raw.get("funds")
    if not isinstance(funds, list) or len(funds) == 0:
        raise ValueError("基金池至少需要一只基金")

    normalized_funds = []
    seen_codes = set()
    for item in funds:
        if not isinstance(item, dict):
            raise ValueError("基金池条目必须是对象")
        code = str(item.get("code", "")).strip()
        if not code:
            raise ValueError("基金 code 不能为空")
        if code in seen_codes:
            raise ValueError(f"重复的基金代码: {code}")
        seen_codes.add(code)
        weight = float(item.get("static_weight", 0))
        if weight <= 0:
            raise ValueError(f"{code} 的 static_weight 必须大于 0")
        normalized_funds.append({
            "code": code,
            "label": str(item.get("label", "")).strip(),
            "static_weight": weight,
        })

    total_weight = sum(f["static_weight"] for f in normalized_funds)
    if abs(total_weight - 1.0) > 1e-6:
        raise ValueError(f"静态权重之和必须为 1.0，当前为 {total_weight:.6f}")

    return {
        "name": str(raw.get("name", "")).strip(),
        "description": str(raw.get("description", "")).strip(),
        "funds": normalized_funds,
    }


def load_fund_pool(strategy_name: str, strategies_dir: str) -> Optional[dict]:
    path = fund_pool_path_for_strategy(strategy_name, strategies_dir)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return normalize_fund_pool(json.load(f))


def load_fund_pool_for_script(script_path: str) -> Optional[dict]:
    path = fund_pool_path_for_script(script_path)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return normalize_fund_pool(json.load(f))


def save_fund_pool(strategy_name: str, strategies_dir: str, pool: dict) -> str:
    normalized = normalize_fund_pool(pool)
    path = fund_pool_path_for_strategy(strategy_name, strategies_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2, ensure_ascii=False)
    return path


def get_fund_codes(pool: dict) -> List[str]:
    return [item["code"] for item in pool["funds"]]


def get_static_weights(pool: dict) -> Dict[str, float]:
    return {item["code"]: item["static_weight"] for item in pool["funds"]}
