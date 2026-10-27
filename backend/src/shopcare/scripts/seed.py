from datetime import datetime

from sqlalchemy.orm import Session

from shopcare.db.models import Order, OrderItem, User


def seed_demo_data(session: Session) -> dict[str, int]:
    users_created = 0
    orders_created = 0
    goods_created = 0

    users = [
        ("U001", "模拟用户甲"),
        ("U002", "模拟用户乙"),
    ]

    for user_id, display_name in users:
        existing = session.get(User, user_id)

        if existing is None:
            session.add(User(id=user_id, display_name=display_name))
            users_created += 1

    session.flush()

    statuses = ["created", "paid", "shipped", "delivered", "cancelled"]

    created_at = datetime(2026, 10, 26, 10, 0, 0)
    delivered_at_fixed = datetime(2026, 10, 30, 12, 0, 0)

    for i in range(1, 21):
        order_id = f"O{i:03d}"
        user_id = "U001" if i <= 10 else "U002"
        status = statuses[(i - 1) % len(statuses)]

        delivered_at = delivered_at_fixed if status == "delivered" else None
        item_data = [
            {
                "id": f"{order_id}-I01",
                "product_name": "无线耳机",
                "category": "数码",
                "quantity": 1,
                "unit_price_cents": 19900,
            },
            {
                "id": f"{order_id}-I02",
                "product_name": "耳机保护套",
                "category": "配件",
                "quantity": 1,
                "unit_price_cents": 2900,
            },
        ]
        total_amount_cents = sum(item["quantity"] * item["unit_price_cents"] for item in item_data)
        exist_order = session.get(Order, order_id)

        if exist_order is None:
            session.add(
                Order(
                    id=order_id,
                    user_id=user_id,
                    status=status,
                    total_amount_cents=total_amount_cents,
                    created_at=created_at,
                    delivered_at=delivered_at,
                )
            )
            orders_created += 1

        session.flush()

        for item in item_data:
            existing_item = session.get(OrderItem, item["id"])

            if existing_item is None:
                session.add(
                    OrderItem(
                        id=item["id"],
                        order_id=order_id,
                        product_name=item["product_name"],
                        category=item["category"],
                        quantity=item["quantity"],
                        unit_price_cents=item["unit_price_cents"],
                    )
                )
                goods_created += 1

        session.flush()

    return {
        "users_created": users_created,
        "orders_created": orders_created,
        "order_items_created": goods_created,
    }


def main(argv: list[str] | None = None) -> int:
    """Initialize local SQLite demo data in one transaction."""
    import argparse
    import json
    import sys
    from pathlib import Path

    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import SQLAlchemyError

    from shopcare.core.config import Settings
    from shopcare.db import build_engine, init_db

    parser = argparse.ArgumentParser(description="初始化 ShopCare 本地模拟数据")
    parser.add_argument("--database-url", help="覆盖配置中的 SQLite 数据库地址")
    args = parser.parse_args(argv)
    engine = None
    try:
        url = make_url(args.database_url or Settings().database_url)
        if url.get_backend_name() != "sqlite":
            parser.error("模拟数据命令当前仅支持本地 SQLite")
        if url.database and url.database != ":memory:":
            database_path = Path(url.database).expanduser().resolve()
            database_path.parent.mkdir(parents=True, exist_ok=True)
            url = url.set(database=str(database_path))
        engine = build_engine(url.render_as_string(hide_password=False))
        init_db(engine)
        with Session(engine) as session, session.begin():
            result = seed_demo_data(session)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, SQLAlchemyError):
        print("初始化失败，请检查配置、数据库访问权限或已有数据约束", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
