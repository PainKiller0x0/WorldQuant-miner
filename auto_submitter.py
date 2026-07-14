import argparse
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any

import database
from improved_alpha_submitter import ImprovedAlphaSubmitter


logger = logging.getLogger("auto_submitter")


def _raw_data(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def load_candidates(
    limit: int,
    min_pass_count: int,
    min_fitness: float,
    min_sharpe: float,
    max_age_hours: float,
) -> list[dict]:
    cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
    with database.get_db() as db:
        rows = db.query(database.Alpha).order_by(database.Alpha.fitness.desc()).limit(limit * 20).all()

        candidates = []
        seen_ids = set()
        for alpha in rows:
            if alpha is None or alpha.is_submitted or alpha.is_failed_on_wq:
                continue
            if alpha.created_at is None or alpha.created_at < cutoff:
                continue
            if (alpha.pass_count or 0) < min_pass_count:
                continue
            if float(alpha.fitness or 0) < min_fitness:
                continue
            if float(alpha.sharpe or 0) < min_sharpe:
                continue

            raw = _raw_data(alpha.raw_data)
            alpha_id = raw.get("alpha_id")
            if not alpha_id and alpha.id and not str(alpha.id).startswith(("legacy:", "local:")):
                alpha_id = alpha.id
            if not alpha_id or alpha_id in seen_ids:
                continue

            seen_ids.add(alpha_id)
            candidates.append({"expression": alpha.expression, "alpha_id": str(alpha_id)})
            if len(candidates) >= limit:
                break
        return candidates


def run_once(args: argparse.Namespace) -> int:
    candidates = load_candidates(
        args.batch_size,
        args.min_pass_count,
        args.min_fitness,
        args.min_sharpe,
        args.max_age_hours,
    )
    if not candidates:
        logger.info("没有符合条件且未提交的 Alpha")
        return 0

    submitter = ImprovedAlphaSubmitter(args.credentials)
    submitted = 0
    for candidate in candidates:
        alpha_id = candidate["alpha_id"]
        expression = candidate["expression"]
        logger.info("准备提交 Alpha %s", alpha_id)
        try:
            if submitter.submit_alpha(alpha_id):
                if database.mark_alpha_submitted(expression):
                    submitted += 1
                    logger.info("Alpha %s 已提交并回写数据库", alpha_id)
                else:
                    logger.error("Alpha %s 已提交，但数据库回写失败", alpha_id)
            else:
                logger.warning("Alpha %s 提交失败，保留为待重试状态", alpha_id)
        except Exception:
            logger.exception("Alpha %s 提交异常", alpha_id)
        if candidate is not candidates[-1]:
            time.sleep(args.delay_seconds)

    logger.info("本轮完成：成功提交 %s/%s", submitted, len(candidates))
    return submitted


def main() -> None:
    parser = argparse.ArgumentParser(description="Submit qualified local Alpha records to WorldQuant Brain")
    parser.add_argument("--credentials", default="./credential.txt")
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--interval-hours", type=float, default=1)
    parser.add_argument("--delay-seconds", type=int, default=30)
    parser.add_argument("--min-pass-count", type=int, default=7)
    parser.add_argument("--min-fitness", type=float, default=1.0)
    parser.add_argument("--min-sharpe", type=float, default=1.25)
    parser.add_argument("--max-age-hours", type=float, default=24)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    if args.once:
        run_once(args)
        return

    while True:
        try:
            run_once(args)
        except Exception:
            logger.exception("自动提交循环失败")
        time.sleep(args.interval_hours * 3600)


if __name__ == "__main__":
    main()
