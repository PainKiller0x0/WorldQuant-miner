# --- database.py v17.0 (Dual Pool & Auto-Clean) ---
import os
import json
import hashlib
import logging
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, String, Integer, Float, Boolean, Text, DateTime, JSON, func
from sqlalchemy.orm import sessionmaker, scoped_session, declarative_base
from contextlib import contextmanager

# 配置
DB_FILE = "wq_miner.db"
DB_URL = f"sqlite:///{DB_FILE}"

logger = logging.getLogger(__name__)
Base = declarative_base()

# --- 数据模型定义 ---

class Alpha(Base):
    __tablename__ = 'alphas'

    # The existing SQLite database uses TEXT ids.  Some historical rows have
    # no id because the old writer relied on integer autoincrement semantics.
    # Keep the database key textual and repair missing values with the
    # migration script before starting the fixed services.
    id = Column(String(128), primary_key=True)
    expression = Column(Text, unique=True, nullable=False, index=True)
    
    # 性能指标
    fitness = Column(Float, default=0.0, index=True)
    sharpe = Column(Float, default=0.0)
    returns = Column(Float, default=0.0)
    turnover = Column(Float, default=0.0)
    margin = Column(Float, default=0.0)
    
    # 检查状态
    checks_summary = Column(String(100))
    pass_count = Column(Integer, default=0)
    fail_count = Column(Integer, default=0)
    
    # 状态标志
    is_submitted = Column(Boolean, default=False, index=True)
    is_failed_on_wq = Column(Boolean, default=False, index=True)
    failure_reason = Column(String(255), nullable=True)
    submitted_timestamp = Column(DateTime, nullable=True)
    
    # 元数据
    created_at = Column(DateTime, default=datetime.utcnow)
    raw_data = Column(JSON)

    def calculate_score(self):
        fit = self.fitness or 0
        sha = self.sharpe or 0
        trn = self.turnover or 0
        pas = self.pass_count or 0
        return fit + (pas * 0.2) + (abs(sha) * 0.3) - (trn * 0.1)

class SystemConfig(Base):
    __tablename__ = 'system_config'
    
    key = Column(String(50), primary_key=True)
    value = Column(JSON, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# --- 数据库引擎与会话 ---

engine = create_engine(
    DB_URL, 
    echo=False, 
    connect_args={'check_same_thread': False}
)

SessionLocal = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))

def init_db():
    Base.metadata.create_all(bind=engine)

@contextmanager
def get_db():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Database Transaction Error: {e}")
        raise
    finally:
        session.close()

# --- 操作函数 ---

def _persistent_alpha_id(alpha_data, expression):
    """Return the WorldQuant id, or a stable local id for legacy records."""
    alpha_id = alpha_data.get('alpha_id')
    if not alpha_id:
        raw_data = alpha_data.get('raw_data')
        if isinstance(raw_data, dict):
            alpha_id = raw_data.get('alpha_id')
    if alpha_id:
        return str(alpha_id)
    digest = hashlib.sha256(expression.encode('utf-8')).hexdigest()[:32]
    return f"local:{digest}"


def add_alpha(alpha_data):
    expr = alpha_data.get('expression')
    if not expr: return False
    alpha_id = _persistent_alpha_id(alpha_data, expr)
    
    checks = alpha_data.get('checks_summary', '')
    import re
    p_match = re.search(r'(\d+)\s+PASS', checks)
    f_match = re.search(r'(\d+)\s+FAIL', checks)
    pass_cnt = int(p_match.group(1)) if p_match else 0
    fail_cnt = int(f_match.group(1)) if f_match else 0
    
    perf = alpha_data.get('performance', {})
    
    with get_db() as db:
        existing = db.query(Alpha).filter(Alpha.expression == expr).first()
        if existing:
            return False 
        if db.get(Alpha, alpha_id) is not None:
            return False
            
        new_alpha = Alpha(
            id=alpha_id,
            expression=expr,
            fitness=float(perf.get('fitness', 0) or 0),
            sharpe=float(perf.get('sharpe', 0) or 0),
            returns=float(perf.get('returns', 0) or 0),
            turnover=float(perf.get('turnover', 0) or 0),
            checks_summary=checks,
            pass_count=pass_cnt,
            fail_count=fail_cnt,
            created_at=datetime.now(timezone.utc),
            raw_data=alpha_data
        )
        db.add(new_alpha)
        return True

def mark_alpha_submitted(expr):
    with get_db() as db:
        alpha = db.query(Alpha).filter(Alpha.expression == expr).first()
        if alpha:
            alpha.is_submitted = True
            alpha.submitted_timestamp = datetime.now(timezone.utc)
            return True
        return False

def mark_alpha_failed(expr, reason):
    with get_db() as db:
        alpha = db.query(Alpha).filter(Alpha.expression == expr).first()
        if alpha:
            alpha.is_failed_on_wq = True
            alpha.failure_reason = reason
            return True
        return False

def trim_alphas(limit_unsubmitted=300, limit_submitted=1000):
    """
    v17.0: 双池修剪逻辑
    1. 优先彻底清洗 'is_failed_on_wq' (提交失败) 的垃圾策略。
    2. 分别检查 '未提交池' 和 '已提交池' 是否超标。
    3. 超标则分别进行末位淘汰 (删除分数最低的)。
    """
    total_deleted = 0
    with get_db() as db:
        # 1. 自动清洗失败品 (Auto-Clean)
        deleted_failed = db.query(Alpha).filter(Alpha.is_failed_on_wq == True).delete()
        if deleted_failed > 0:
            logger.info(f"[DB] 已自动清洗 {deleted_failed} 个提交失败的策略。")
        total_deleted += deleted_failed
        
        # 2. 修剪 [未提交池] (Potential Pool)
        count_unsub = db.query(func.count(Alpha.id)).filter(
            Alpha.is_submitted == False, 
            Alpha.is_failed_on_wq == False
        ).scalar()
        
        if count_unsub > limit_unsubmitted:
            to_del = count_unsub - limit_unsubmitted
            # 找出最低分的 N 个
            subquery = db.query(Alpha.id)\
                .filter(Alpha.is_submitted == False)\
                .filter(Alpha.is_failed_on_wq == False)\
                .order_by(Alpha.fitness.asc())\
                .limit(to_del)\
                .all()
            ids = [r[0] for r in subquery]
            if ids:
                db.query(Alpha).filter(Alpha.id.in_(ids)).delete(synchronize_session=False)
                total_deleted += len(ids)

        # 3. 修剪 [已提交池] (Honor Pool)
        count_sub = db.query(func.count(Alpha.id)).filter(Alpha.is_submitted == True).scalar()
        
        if count_sub > limit_submitted:
            to_del = count_sub - limit_submitted
            subquery = db.query(Alpha.id)\
                .filter(Alpha.is_submitted == True)\
                .order_by(Alpha.fitness.asc())\
                .limit(to_del)\
                .all()
            ids = [r[0] for r in subquery]
            if ids:
                db.query(Alpha).filter(Alpha.id.in_(ids)).delete(synchronize_session=False)
                total_deleted += len(ids)
                
    return total_deleted

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
