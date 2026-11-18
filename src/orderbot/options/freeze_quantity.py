from dataclasses import dataclass

from orderbot.broker.instruments import Instrument


class FreezeError(ValueError):
    """No valid whole-lot order plan for the supplied contract and quantity."""


@dataclass(frozen=True, slots=True)
class FreezePlan:
    children: tuple[int, ...]
    max_order_quantity: int
    exchange_cap: int
    broker_raw_threshold: int


def split_quantity(quantity: int, contract: Instrument, exchange_cap: int) -> FreezePlan:
    lot = contract.lot_size
    raw = contract.raw_freeze_quantity
    if type(lot) is not int or lot <= 0:
        raise FreezeError("Contract lot size is missing or invalid.")
    if type(quantity) is not int or quantity <= 0 or quantity % lot:
        raise FreezeError("Order quantity must be a positive multiple of the contract lot size.")
    if type(exchange_cap) is not int or exchange_cap <= 0 or type(raw) is not int or raw <= 1:
        raise FreezeError("Both exchange and broker freeze limits are required.")
    capacity = min(exchange_cap, raw - 1) // lot * lot
    if not capacity:
        raise FreezeError("A whole lot exceeds the freeze limits.")
    full, trailing = divmod(quantity, capacity)
    children = (capacity,) * full + ((trailing,) if trailing else ())
    return FreezePlan(children, capacity, exchange_cap, raw)
