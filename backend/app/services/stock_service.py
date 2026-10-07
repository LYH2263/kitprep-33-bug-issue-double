"""出库同一口径：备料台领料与库存页出库共用同一套扣账逻辑。

口径约定：
- 账面结存（stock_qty）只在真正出库时扣一次，扣账统一走 _debit_stock；
- 生成备料单时只增加占用（reserved_qty），不动账面；
- 领料把这张单上的预占转成出库：账面按本单各行的量扣一次，
  占用只减本单那一笔（多张单并存时不清空别人的占用）；
- 本模块只改对象不提交事务，由调用方统一 commit / rollback；
  先整体校验、再统一落账、最后才改单状态，任何一步失败，
  状态、占用、账面全部退回，旧存档单一律不碰。
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.models import Ingredient, PrepRun

EPS = 1e-9


class StockError(Exception):
    """库存操作失败（余额不足 / 重复领料 / 单不存在）。调用方应回滚事务。"""


def _ensure_balance(ing: Ingredient | None, qty: float) -> Ingredient:
    if ing is None:
        raise StockError("原料不存在")
    if ing.stock_qty + EPS < qty:
        raise StockError(f"账面结存不足：{ing.name} 现存 {ing.stock_qty}，需出 {qty}")
    return ing


def _debit_stock(ing: Ingredient, qty: float) -> None:
    """唯一的账面扣减点：结存只减这一笔。调用方须先 _ensure_balance。"""
    ing.stock_qty = round(ing.stock_qty - qty, 3)


def stock_out_once(db: Session, ingredient_id: int, qty: float) -> Ingredient:
    """库存页直接出库：只扣账面一笔。

    不关联任何备料单，因此不动占用——占用始终由具体备料单的
    生成 / 领料负责，两条出库路径的账面扣减是同一个 _debit_stock。
    """
    if qty <= 0:
        raise StockError("出库数量必须大于 0")
    ing = db.get(Ingredient, ingredient_id, with_for_update=True)
    _ensure_balance(ing, qty)
    _debit_stock(ing, qty)
    return ing


def issue_prep_run(db: Session, run_id: int) -> PrepRun:
    """把这张备料单的预占转成真正出库。

    先锁单并整体校验，再统一落账，最后改角标：已领过的单直接失败，
    占用和账面都不再动；占用只按本单各行那一笔清，账面只按同一笔扣一次；
    只动这一张单，更早存档的单（状态与存档内容）不受影响。
    """
    run = db.get(PrepRun, run_id, with_for_update=True)
    if run is None:
        raise StockError("备料单不存在")
    if run.status == "issued":
        raise StockError("该备料单已领料，不能重复出库")
    lines = json.loads(run.result_json).get("prep_lines", [])
    ings = {l["ingredient_id"]: db.get(Ingredient, l["ingredient_id"], with_for_update=True)
            for l in lines}
    for l in lines:  # 先全部校验通过，任何一行不足都一笔不扣
        _ensure_balance(ings[l["ingredient_id"]], l["need_qty"])
    for l in lines:  # 校验通过后统一落账：账面一笔、占用只清本单那一笔
        ing = ings[l["ingredient_id"]]
        need = float(l["need_qty"])
        _debit_stock(ing, need)
        ing.reserved_qty = round(max(0.0, float(ing.reserved_qty or 0.0) - need), 3)
    run.status = "issued"  # 角标最后才改，失败时随事务整体退回
    run.issued_at = datetime.utcnow()
    return run
