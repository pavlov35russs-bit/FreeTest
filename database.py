import json
import os
from datetime import datetime, timedelta

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    delete,
    func,
    select,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DB_DIR = os.getenv("DB_DIR", "/data")
os.makedirs(DB_DIR, exist_ok=True)

DB_PATH = os.path.join(DB_DIR, "bot.db")
engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}")
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

MOSCOW_OFFSET = timedelta(hours=3)
QUESTIONS_FILE = os.path.join(os.path.dirname(__file__), "questions.json")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=True)
    first_name: Mapped[str] = mapped_column(String, nullable=True)
    subject: Mapped[str] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String, default="free")
    daily_count: Mapped[int] = mapped_column(Integer, default=0)
    last_reset: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    subscription_until: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String)
    topic: Mapped[str] = mapped_column(String, default="Общее")
    text: Mapped[str] = mapped_column(String)
    option_a: Mapped[str] = mapped_column(String)
    option_b: Mapped[str] = mapped_column(String)
    option_c: Mapped[str] = mapped_column(String)
    option_d: Mapped[str] = mapped_column(String)
    correct: Mapped[str] = mapped_column(String)
    explanation: Mapped[str] = mapped_column(String, default="")


class Referral(Base):
    __tablename__ = "referrals"
    invited_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger)
    rewarded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserProgress(Base):
    __tablename__ = "user_progress"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    question_id: Mapped[int] = mapped_column(Integer)
    topic: Mapped[str] = mapped_column(String, default="Общее")
    correct: Mapped[bool] = mapped_column(Boolean)
    answered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


def _moscow_date(dt: datetime):
    return (dt + MOSCOW_OFFSET).date()


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_questions()


async def seed_questions(force: bool = False):
    async with SessionLocal() as session:
        if force:
            await session.execute(delete(Question))
            await session.commit()
        else:
            result = await session.execute(select(Question).limit(1))
            if result.scalar_one_or_none():
                return

        if not os.path.exists(QUESTIONS_FILE):
            return

        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        questions = [Question(**item) for item in data]
        session.add_all(questions)
        await session.commit()


async def get_or_create_user(tg_id, username, first_name):
    async with SessionLocal() as session:
        user = await session.get(User, tg_id)
        if not user:
            user = User(id=tg_id, username=username, first_name=first_name)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user


async def get_user(tg_id):
    async with SessionLocal() as session:
        return await session.get(User, tg_id)


async def set_subject(tg_id, subject):
    async with SessionLocal() as session:
        user = await session.get(User, tg_id)
        if user:
            user.subject = subject
            await session.commit()


async def get_random_question(subject):
    async with SessionLocal() as session:
        result = await session.execute(
            select(Question)
            .where(Question.subject == subject)
            .order_by(func.random())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def get_question(qid):
    async with SessionLocal() as session:
        return await session.get(Question, qid)


async def count_questions():
    async with SessionLocal() as session:
        result = await session.execute(select(func.count()).select_from(Question))
        return result.scalar_one()


async def count_users():
    async with SessionLocal() as session:
        result = await session.execute(select(func.count()).select_from(User))
        return result.scalar_one()


async def get_all_user_ids():
    async with SessionLocal() as session:
        result = await session.execute(select(User.id))
        return [row[0] for row in result.all()]


def _refresh_role(user: User):
    if user.role == "premium":
        if not user.subscription_until or user.subscription_until < datetime.utcnow():
            user.role = "free"


async def check_and_increment_daily(tg_id, limit=20):
    async with SessionLocal() as session:
        user = await session.get(User, tg_id)
        if not user:
            return False, 0

        now = datetime.utcnow()
        if _moscow_date(user.last_reset) != _moscow_date(now):
            user.daily_count = 0
            user.last_reset = now

        _refresh_role(user)

        if user.role == "premium":
            await session.commit()
            return True, user.daily_count

        if user.daily_count >= limit:
            await session.commit()
            return False, user.daily_count

        user.daily_count += 1
        user.last_reset = now
        await session.commit()
        return True, user.daily_count


async def activate_subscription(tg_id, days):
    async with SessionLocal() as session:
        user = await session.get(User, tg_id)
        if not user:
            return None
        now = datetime.utcnow()
        base = (
            user.subscription_until
            if user.subscription_until and user.subscription_until > now
            else now
        )
        user.subscription_until = base + timedelta(days=days)
        user.role = "premium"
        await session.commit()
        await session.refresh(user)
        return user.subscription_until


async def create_referral(invited_id, referrer_id):
    async with SessionLocal() as session:
        existing = await session.get(Referral, invited_id)
        if existing:
            return False
        if invited_id == referrer_id:
            return False
        referrer = await session.get(User, referrer_id)
        if not referrer:
            return False
        ref = Referral(invited_id=invited_id, referrer_id=referrer_id)
        session.add(ref)
        await session.commit()
        return True


async def reward_referrer_if_pending(invited_id):
    async with SessionLocal() as session:
        ref = await session.get(Referral, invited_id)
        if not ref or ref.rewarded:
            return None
        referrer = await session.get(User, ref.referrer_id)
        if not referrer:
            ref.rewarded = True
            await session.commit()
            return None
        now = datetime.utcnow()
        base = (
            referrer.subscription_until
            if referrer.subscription_until and referrer.subscription_until > now
            else now
        )
        referrer.subscription_until = base + timedelta(days=7)
        referrer.role = "premium"
        ref.rewarded = True
        await session.commit()
        await session.refresh(referrer)
        return referrer


async def log_answer(tg_id, question_id, topic, correct):
    async with SessionLocal() as session:
        entry = UserProgress(
            user_id=tg_id,
            question_id=question_id,
            topic=topic or "Общее",
            correct=correct,
        )
        session.add(entry)
        await session.commit()


async def get_user_stats(tg_id):
    async with SessionLocal() as session:
        total = await session.execute(
            select(func.count())
            .select_from(UserProgress)
            .where(UserProgress.user_id == tg_id)
        )
        total = total.scalar_one()

        correct = await session.execute(
            select(func.count())
            .select_from(UserProgress)
            .where(UserProgress.user_id == tg_id, UserProgress.correct.is_(True))
        )
        correct = correct.scalar_one()

        topics = await session.execute(
            select(
                UserProgress.topic,
                func.count().label("total"),
                func.sum(func.cast(UserProgress.correct, Integer)).label("correct"),
            )
            .where(UserProgress.user_id == tg_id)
            .group_by(UserProgress.topic)
        )
        topics_data = [
            {"topic": row.topic, "total": row.total, "correct": row.correct or 0}
            for row in topics.all()
        ]

        return {"total": total, "correct": correct, "topics": topics_data}
