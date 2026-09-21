"""安全清理 backend/files 里没有任何消息引用的孤儿图片。

与「按时间删」不同，本脚本只删除**数据库里查不到引用**的文件，
不会误删仍被历史消息引用的图片；同时会报告「被引用但文件已丢失」的情况。

用法：
    .venv/bin/python scripts/cleanup_files.py            # 预览，不删任何东西
    .venv/bin/python scripts/cleanup_files.py --delete   # 确认后执行删除
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402
from app.files import FILES_DIR  # noqa: E402


async def referenced_names() -> set[str]:
    """收集所有消息 meta.attachments 里出现过的文件名。"""
    conn = await asyncpg.connect(settings.database_url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        rows = await conn.fetch("SELECT meta FROM messages WHERE meta::text LIKE '%attachments%'")
    finally:
        await conn.close()

    names: set[str] = set()
    for row in rows:
        meta = row["meta"] if isinstance(row["meta"], dict) else json.loads(row["meta"])
        for name in meta.get("attachments") or []:
            if name:
                names.add(name)
    return names


async def main() -> None:
    delete = "--delete" in sys.argv
    FILES_DIR.mkdir(parents=True, exist_ok=True)

    referenced = await referenced_names()
    on_disk = {path.name for path in FILES_DIR.iterdir() if path.is_file()}
    orphans = sorted(on_disk - referenced)
    missing = sorted(referenced - on_disk)

    print(f"磁盘文件 {len(on_disk)} 个，被消息引用 {len(referenced)} 个")

    if missing:
        print(f"\n⚠️  被引用但文件已丢失 {len(missing)} 个（无法恢复，前端会显示「图片已失效」占位）：")
        for name in missing:
            print(f"    ❌ {name}")

    if not orphans:
        print("\n没有孤儿文件，无需清理。")
        return

    print(f"\n孤儿文件 {len(orphans)} 个（没有任何消息引用）：")
    for name in orphans:
        print(f"    {name}  ({(FILES_DIR / name).stat().st_size} B)")

    if not delete:
        print("\n以上为预览，未删除任何文件。确认无误后加 --delete 执行。")
        return

    for name in orphans:
        (FILES_DIR / name).unlink()
    print(f"\n已删除 {len(orphans)} 个孤儿文件。")


if __name__ == "__main__":
    asyncio.run(main())
