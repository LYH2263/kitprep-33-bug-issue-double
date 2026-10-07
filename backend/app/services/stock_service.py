"""出库同一口径：备料台领料与库存页出库共用同一套扣账逻辑。

口径约定：
- 账面结存（stock_qty）只在真正出库时扣一次；
- 生成备料单时只增加占用（reserved_qty），不动账面；
- 领料把这张单上的预占转成出库：占用按该单那一笔清掉，账面按同一笔扣一次；
- 库存页直接出库不挂备料单，只扣账面、不碰占用；
- 本模块只改对象不提交事务，由调用方统一 commit / rollback，
  任何一步失败，状态、占用、账面全部退回。
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.models import Ingredient, PrepRun

EPS = 1e-9


class StockError(Exception):
    """库存操作失败（余额不足 / 重复领料 / 单不存在）。调用方应回滚事务。"""


def _get_locked(db: Session, ingredient_id: int) -> Ingredient:
    ing = db.get(Ingredient, ingredient_id, with_for_update=True)
    if ing is None:
        raise StockError("原料不存在")
    return ing


def _ensure_balance(ing: Ingredient, qty: float) -> None:
    if ing.stock_qty + EPS < qty:
        raise StockError(f"账面结存不足：{ing.name} 现存 {ing.stock_qty}，需出 {qty}")


def _debit_stock(ing: Ingredient, qty: float) -> None:
    """账面只扣这一笔，不碰占用（占用的清理由领料流程按单处理）。"""
    ing.stock_qty = round(ing.stock_qty - qty, 3)


def stock_out_once(db: Session, ingredient_id: int, qty: float) -> Ingredient:
    """库存页直接出库：只扣账面结存一次，不夹带任何占用调整。"""
    if qty <= 0:
        raise StockError("出库数量必须大于 0")
    ing = _get_locked(db, ingredient_id)
    _ensure_balance(ing, qty)
    _debit_stock(ing, qty)
    return ing


def issue_prep_run(db: Session, run_id: int) -> PrepRun:
    """把这张备料单的预占转成真正出库。

    先整体校验再统一落账，避免扣到一半；校验通过后对每个原料一次性
    完成「占用按本单那一笔清掉 + 账面按同一笔扣一次」。
    已领过的单直接失败，占用和账面都不再动；行锁只锁这一张单，
    更早存档的单不被碰。状态最后才翻，任何一步出错由调用方回滚，
    角标、占用、账面一起退回。
    """
    run = db.get(PrepRun, run_id, with_for_update=True)
    if run is None:
        raise StockError("备料单不存在")
    if run.status == "issued":
        raise StockError("该备料单已领料，不能重复出库")

    lines = json.loads(run.result_json).get("prep_lines", [])
    ings = {l["ingredient_id"]: _get_locked(db, l["ingredient_id"]) for l in lines}
    need: dict[int, float] = {}
    for l in lines:  # 同一原料在单内合并成一笔
        qty = float(l["need_qty"])
        if qty <= 0:
            raise StockError("备料数量必须大于 0")
        need[l["ingredient_id"]] = round(need.get(l["ingredient_id"], 0.0) + qty, 3)

    for ingredient_id, qty in need.items():  # 先全部校验通过
        _ensure_balance(ings[ingredient_id], qty)
    for ingredient_id, qty in need.items():  # 再统一落账：一笔扣款 + 一笔清占用
        ing = ings[ingredient_id]
        _debit_stock(ing, qty)
        ing.reserved_qty = round(max(0.0, float(ing.reserved_qty or 0.0) - qty), 3)

    run.status = "issued"
    run.issued_at = datetime.utcnow()
    return run
