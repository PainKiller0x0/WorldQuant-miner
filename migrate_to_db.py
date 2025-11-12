# --- migrate_to_db.py (v15.1 Fixed Timestamps) ---
import json
import os
import sys
from datetime import datetime
from database import init_db, get_db, Alpha

def load_json(filepath):
    if not os.path.exists(filepath): return {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except: return {}

def parse_timestamp(ts_str):
    """尝试解析多种格式的时间戳，失败则返回当前时间"""
    if not ts_str: return datetime.utcnow()
    try:
        # 尝试 ISO 格式 (e.g., 2023-01-01T12:00:00)
        return datetime.fromisoformat(ts_str)
    except:
        try:
            # 尝试常见日志格式 (e.g., 2023-01-01 12:00:00)
            return datetime.strptime(str(ts_str), "%Y-%m-%d %H:%M:%S")
        except:
            return datetime.utcnow()

def migrate():
    print("🚀 开始 v15.1 数据库迁移 (时间戳修复版)...")
    
    init_db()
    
    print("📂 读取源数据...")
    hopeful_data = load_json("hopeful_alphas.json")
    submitted_data = load_json("submitted_alphas.json")
    failed_list = load_json("submission_failure_log.json")
    
    hopeful_alphas = hopeful_data.get("alphas", []) if isinstance(hopeful_data, dict) else []
    
    # 构建 submitted 映射 (timestamp)
    submitted_map = {}
    if isinstance(submitted_data, dict):
        for expr, info in submitted_data.items():
            if isinstance(info, dict):
                submitted_map[expr] = info.get('manual_timestamp')
            else:
                submitted_map[expr] = None # 旧格式可能没有时间戳

    # 构建 failed 映射 (reason & timestamp)
    failed_map = {}
    if isinstance(failed_list, list):
        for item in failed_list:
            if isinstance(item, dict) and "expression" in item:
                failed_map[item["expression"]] = {
                    "reason": item.get("reason", "UNKNOWN"),
                    "timestamp": item.get("timestamp")
                }

    print(f"📊 统计: Hopeful {len(hopeful_alphas)}, Submitted {len(submitted_map)}, Failed {len(failed_map)}")
    
    with get_db() as db:
        processed_expressions = set()
        
        # 预加载防止重复
        existing = db.query(Alpha.expression).all()
        for (expr,) in existing:
            processed_expressions.add(expr)

        count = 0
        skipped = 0
        
        # --- 1. 处理 Hopeful Alphas ---
        print("🔄 正在导入 Hopeful Alphas (保留原始时间)...")
        for alpha in hopeful_alphas:
            expr = alpha.get('expression')
            if not expr: continue
            if expr in processed_expressions:
                skipped += 1
                continue
            
            # 解析关键字段
            perf = alpha.get('performance', {})
            checks = alpha.get('checks_summary', '')
            raw_ts = alpha.get('timestamp') # <--- 获取原始生成时间
            created_at_dt = parse_timestamp(raw_ts)
            
            import re
            p_match = re.search(r'(\d+)\s+PASS', checks)
            f_match = re.search(r'(\d+)\s+FAIL', checks)
            
            # 状态判断
            is_sub = expr in submitted_map
            is_fail = expr in failed_map
            
            fail_info = failed_map.get(expr, {})
            fail_reason = fail_info.get('reason')
            
            # 确定提交时间
            sub_ts = None
            if is_sub:
                sub_ts_str = submitted_map.get(expr)
                sub_ts = parse_timestamp(sub_ts_str) if sub_ts_str else created_at_dt
            
            new_alpha = Alpha(
                expression=expr,
                fitness=float(perf.get('fitness', 0) or 0),
                sharpe=float(perf.get('sharpe', 0) or 0),
                returns=float(perf.get('returns', 0) or 0),
                turnover=float(perf.get('turnover', 0) or 0),
                checks_summary=checks,
                pass_count=int(p_match.group(1)) if p_match else 0,
                fail_count=int(f_match.group(1)) if f_match else 0,
                
                is_submitted=is_sub,
                submitted_timestamp=sub_ts, # <--- 写入提交时间
                
                is_failed_on_wq=is_fail,
                failure_reason=fail_reason,
                
                created_at=created_at_dt,   # <--- 写入原始生成时间
                raw_data=alpha
            )
            db.add(new_alpha)
            processed_expressions.add(expr)
            count += 1
        
        # --- 2. 处理孤儿策略 ---
        print("🔍 恢复孤儿策略...")
        for expr, ts_str in submitted_map.items():
            if expr in processed_expressions: continue
            
            orphan_ts = parse_timestamp(ts_str)
            orphan = Alpha(
                expression=expr, 
                is_submitted=True, 
                submitted_timestamp=orphan_ts, # <--- 孤儿也有提交时间
                created_at=orphan_ts,          # 孤儿没有生成时间，暂用提交时间代替
                raw_data={"source": "migration_orphan"}
            )
            db.add(orphan)
            processed_expressions.add(expr)
            count += 1

        print("💾 正在提交事务...")
        
    print(f"✅ 修复完成! 总计导入: {count}, 跳过重复: {skipped}")

if __name__ == "__main__":
    migrate()