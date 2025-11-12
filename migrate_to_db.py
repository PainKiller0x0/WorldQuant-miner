# --- migrate_to_db.py (Fixed v2) ---
import json
import os
import sys
from database import init_db, get_db, Alpha, SystemConfig

def load_json(filepath):
    if not os.path.exists(filepath): return {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except: return {}

def migrate():
    print("🚀 开始 v15.0 数据库迁移 (修复版)...")
    
    # 1. 初始化数据库
    init_db()
    
    # 2. 读取所有 JSON 文件
    print("📂 读取旧版 JSON 文件...")
    hopeful_data = load_json("hopeful_alphas.json")
    submitted_data = load_json("submitted_alphas.json")
    failed_list = load_json("submission_failure_log.json")
    
    # 转换数据结构
    hopeful_alphas = hopeful_data.get("alphas", []) if isinstance(hopeful_data, dict) else []
    submitted_set = set(submitted_data.keys()) if isinstance(submitted_data, dict) else set()
    failed_map = {}
    if isinstance(failed_list, list):
        for item in failed_list:
            if isinstance(item, dict) and "expression" in item:
                failed_map[item["expression"]] = item.get("reason", "UNKNOWN")

    print(f"📊 统计: Hopeful池 {len(hopeful_alphas)} 条, 已提交 {len(submitted_set)} 条, 失败记录 {len(failed_map)} 条")
    
    # 3. 写入数据库
    with get_db() as db:
        # 关键修复: 使用内存集合跟踪本事务中已添加的表达式，防止重复添加
        processed_expressions = set()
        
        # 先加载数据库里已有的（防止二次运行报错）
        existing = db.query(Alpha.expression).all()
        for (expr,) in existing:
            processed_expressions.add(expr)

        count = 0
        skipped = 0
        
        # --- 第一轮: 处理 Hopeful Alphas (完整信息) ---
        print("🔄 正在导入 Hopeful Alphas...")
        for alpha in hopeful_alphas:
            expr = alpha.get('expression')
            if not expr: continue
            
            # 如果已经处理过，跳过
            if expr in processed_expressions:
                skipped += 1
                continue
            
            # 解析数据
            perf = alpha.get('performance', {})
            checks = alpha.get('checks_summary', '')
            import re
            p_match = re.search(r'(\d+)\s+PASS', checks)
            f_match = re.search(r'(\d+)\s+FAIL', checks)
            
            is_sub = expr in submitted_set
            is_fail = expr in failed_map
            fail_reason = failed_map.get(expr)
            
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
                is_failed_on_wq=is_fail,
                failure_reason=fail_reason,
                raw_data=alpha
            )
            db.add(new_alpha)
            processed_expressions.add(expr) # 标记为已处理
            count += 1
            
            if count % 100 == 0:
                print(f"   ...已入库 {count} 条")
        
        # --- 第二轮: 处理 Orphan Alphas (submitted 但不在 hopeful 中) ---
        print("🔍 正在检查并恢复孤儿策略...")
        orphan_count = 0
        for expr in submitted_set:
            # 关键修复: 直接查内存集合，而不是查还没 commit 的数据库
            if expr in processed_expressions:
                continue 
            
            # 创建一个占位 Alpha
            orphan = Alpha(expression=expr, is_submitted=True, raw_data={"source": "migration_orphan"})
            db.add(orphan)
            processed_expressions.add(expr) # 标记为已处理
            
            count += 1
            orphan_count += 1
            # print(f"   [修复] 恢复孤儿策略: {expr[:30]}...") 

        if orphan_count > 0:
            print(f"   ✅ 成功恢复了 {orphan_count} 个孤儿策略。")

        print("💾 正在提交事务 (这可能需要几秒钟)...")
        
    print(f"✅ 迁移全部完成! 总计导入: {count}, 跳过重复: {skipped}")
    print(f"🎉 数据库文件生成于: {os.path.abspath('wq_miner.db')}")

if __name__ == "__main__":
    migrate()