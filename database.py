# --- database.py v15.0 (Database Core) ---
import os
import json
import logging
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, String, Integer, Float, Boolean, Text, DateTime, JSON, func
from sqlalchemy.orm import sessionmaker, scoped_session, declarative_base
from contextlib import contextmanager

# 配置
DB_FILE = "wq_miner.db"
DB_URL = f"sqlite:///{DB_FILE}"

# 初始化
logger = logging.getLogger(__name__)
Base = declarative_base()

# --- 数据模型定义 ---

class Alpha(Base):
    """
    存储所有生成的 Alpha (对应 hopeful_alphas.json)
    """
    __tablename__ = 'alphas'

    id = Column(Integer, primary_key=True, autoincrement=True)
    expression = Column(Text, unique=True, nullable=False, index=True) # 表达式 (唯一)
    
    # 性能指标
    fitness = Column(Float, default=0.0, index=True)
    sharpe = Column(Float, default=0.0)
    returns = Column(Float, default=0.0)
    turnover = Column(Float, default=0.0)
    margin = Column(Float, default=0.0)
    
    # 检查状态
    checks_summary = Column(String(100)) # e.g., "10 PASS, 0 FAIL"
    pass_count = Column(Integer, default=0)
    fail_count = Column(Integer, default=0)
    
    # 状态标志
    is_submitted = Column(Boolean, default=False, index=True)      # 是否已提交
    is_failed_on_wq = Column(Boolean, default=False, index=True)   # 是否被标记为失败
    failure_reason = Column(String(255), nullable=True)            # 失败原因
    submitted_timestamp = Column(DateTime, nullable=True)          # 提交时间
    
    # 元数据
    created_at = Column(DateTime, default=datetime.utcnow)
    raw_data = Column(JSON) # 存储原始的完整 JSON 数据 (备份用)

    def to_dict(self):
        return {
            "expression": self.expression,
            "fitness": self.fitness,
            "sharpe": self.sharpe,
            "returns": self.returns,
            "turnover": self.turnover,
            "checks_summary": self.checks_summary,
            "timestamp": self.created_at.isoformat() if self.created_at else None,
            "is_submitted": self.is_submitted,
            "is_failed_on_wq": self.is_failed_on_wq,
            "failure_reason": self.failure_reason,
            "dashboard_score": self.calculate_score()
        }

    def calculate_score(self):
        # 移植 Dashboard 的评分逻辑
        fit = self.fitness or 0
        sha = self.sharpe or 0
        trn = self.turnover or 0
        pas = self.pass_count or 0
        return fit + (pas * 0.2) + (abs(sha) * 0.3) - (trn * 0.1)

class SystemConfig(Base):
    """
    存储系统配置 (替代 system_config.json)
    """
    __tablename__ = 'system_config'
    
    key = Column(String(50), primary_key=True) # e.g., "global_config"
    value = Column(JSON, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# --- 数据库引擎与会话 ---

engine = create_engine(
    DB_URL, 
    echo=False, # Set True for debug SQL
    connect_args={'check_same_thread': False} # SQLite specific for multithreading
)

# 线程安全的 Session 工厂
SessionLocal = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))

def init_db():
    """初始化数据库表"""
    Base.metadata.create_all(bind=engine)
    logger.info(f"Database initialized at {DB_FILE}")

@contextmanager
def get_db():
    """
    上下文管理器，用于安全地获取和关闭 Session
    使用方法:
    with get_db() as db:
        db.query(...)
    """
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

# --- 便捷操作函数 (供 utils.py 调用) ---

def add_alpha(alpha_data):
    """插入一个新的 Alpha，如果存在则忽略或更新"""
    expr = alpha_data.get('expression')
    if not expr: return False
    
    # 解析 checks
    checks = alpha_data.get('checks_summary', '')
    import re
    p_match = re.search(r'(\d+)\s+PASS', checks)
    f_match = re.search(r'(\d+)\s+FAIL', checks)
    pass_cnt = int(p_match.group(1)) if p_match else 0
    fail_cnt = int(f_match.group(1)) if f_match else 0
    
    perf = alpha_data.get('performance', {})
    
    with get_db() as db:
        # Check exist
        existing = db.query(Alpha).filter(Alpha.expression == expr).first()
        if existing:
            return False # 已存在
            
        new_alpha = Alpha(
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

if __name__ == "__main__":
    # 测试初始化
    logging.basicConfig(level=logging.INFO)
    init_db()