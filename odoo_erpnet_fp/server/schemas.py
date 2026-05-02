"""
Pydantic schemas matching ErpNet.FP Net.FP HTTP protocol 1:1.

Source of truth: github.com/rosenvladimirov/ErpNet.FP/PROTOCOL.md
This module defines the request and response shapes verbatim so existing
clients (in particular `l10n_bg_erp_net_fp` Odoo addon) work without any
changes against this server.

Field names use camelCase per the upstream JSON convention. Internal
Python uses snake_case via field aliases.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ─── Enums (string values shipped over the wire) ──────────────────────


class PaymentType(str, Enum):
    """ErpNet.FP payment types. NRA mapping in trailing comment.

    Per PROTOCOL.md §"payments". The order matches the device's
    programmable payment slots — concrete vendor mapping happens in
    `adapters/payment_type.py`.
    """

    cash = "cash"  # NRA: SCash
    check = "check"  # NRA: SChecks
    card = "card"  # NRA: SCards
    coupons = "coupons"  # NRA: ST
    ext_coupons = "ext-coupons"  # NRA: SOT
    packaging = "packaging"  # NRA: SP
    internal_usage = "internal-usage"  # NRA: SSelf
    damage = "damage"  # NRA: SDmg
    bank = "bank"  # NRA: SW
    reserved1 = "reserved1"  # NRA: SR1
    reserved2 = "reserved2"  # NRA: SR2


class PriceModifierType(str, Enum):
    discount_percent = "discount-percent"
    discount_amount = "discount-amount"
    surcharge_percent = "surcharge-percent"
    surcharge_amount = "surcharge-amount"


class ItemType(str, Enum):
    sale = "sale"  # default — can be omitted
    discount_amount = "discount-amount"  # subtotal modifier (amount)
    surcharge_amount = "surcharge-amount"
    comment = "comment"  # `# ...` line
    footer_comment = "footer-comment"  # printed after payment area


class MessageType(str, Enum):
    info = "info"
    warning = "warning"
    error = "error"
    reserved = "reserved"


# ─── Common / shared models ──────────────────────────────────────────


class _CamelModel(BaseModel):
    """Base model that accepts either camelCase or snake_case input."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class StatusMessage(_CamelModel):
    type: MessageType
    code: Optional[str] = None
    text: str = ""


class DeviceInfo(_CamelModel):
    """Returned by GET /printers and GET /printers/{id}."""

    uri: str = ""
    serial_number: str = Field("", alias="serialNumber")
    fiscal_memory_serial_number: str = Field("", alias="fiscalMemorySerialNumber")
    manufacturer: str = ""
    model: str = ""
    firmware_version: str = Field("", alias="firmwareVersion")
    item_text_max_length: int = Field(36, alias="itemTextMaxLength")
    comment_text_max_length: int = Field(42, alias="commentTextMaxLength")
    operator_password_max_length: int = Field(8, alias="operatorPasswordMaxLength")
    tax_identification_number: str = Field("", alias="taxIdentificationNumber")
    supported_payment_types: list[str] = Field(
        default_factory=list, alias="supportedPaymentTypes"
    )


class DeviceStatusWithDateTime(_CamelModel):
    """Returned by GET /printers/{id}/status."""

    ok: bool = True
    device_date_time: Optional[str] = Field(None, alias="deviceDateTime")
    messages: list[StatusMessage] = Field(default_factory=list)


# ─── Receipt items (polymorphic by `type`) ───────────────────────────


class _ItemBase(_CamelModel):
    type: Optional[ItemType] = None


class SaleItem(_ItemBase):
    type: Literal[ItemType.sale, None] = ItemType.sale
    text: str = ""
    quantity: float = 1.0
    unit_price: float = Field(..., alias="unitPrice")
    tax_group: int = Field(..., alias="taxGroup", ge=1, le=8)
    department: Optional[int] = None
    price_modifier_value: Optional[float] = Field(None, alias="priceModifierValue")
    price_modifier_type: Optional[PriceModifierType] = Field(
        None, alias="priceModifierType"
    )


class SubtotalAmountItem(_ItemBase):
    """Used for subtotal-level discount-amount / surcharge-amount."""

    type: Literal[ItemType.discount_amount, ItemType.surcharge_amount]
    amount: float


class CommentItem(_ItemBase):
    type: Literal[ItemType.comment, ItemType.footer_comment]
    text: str


# Discriminated union — pydantic picks the right model by `type`
ReceiptItem = Annotated[
    Union[SaleItem, SubtotalAmountItem, CommentItem],
    Field(discriminator="type"),
]


# ─── Payments ────────────────────────────────────────────────────────


class Payment(_CamelModel):
    amount: float
    payment_type: PaymentType = Field(PaymentType.cash, alias="paymentType")


# ─── Receipt envelope (POST /printers/{id}/receipt body) ─────────────


class Receipt(_CamelModel):
    """Body of POST /printers/{id}/receipt."""

    unique_sale_number: str = Field(..., alias="uniqueSaleNumber")
    operator: Optional[str] = None
    operator_password: Optional[str] = Field(None, alias="operatorPassword")
    items: list[ReceiptItem] = Field(default_factory=list)
    payments: list[Payment] = Field(default_factory=list)
    info: Optional[dict] = None  # vendor-specific extras

    @model_validator(mode="before")
    @classmethod
    def _default_item_type_to_sale(cls, data):
        # Pydantic discriminated unions REQUIRE the discriminator field
        # to be present in input. Existing ErpNet.FP clients (in
        # particular l10n_bg_erp_net_fp Odoo addon) omit `type` for
        # sale lines because PROTOCOL.md says "sale is the default,
        # type may be omitted". Add `type=sale` to any item that has
        # no `type` so it routes to SaleItem.
        if isinstance(data, dict):
            items = data.get("items")
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and "type" not in item:
                        item["type"] = "sale"
        return data


class ReversalReceipt(Receipt):
    """Body of POST /printers/{id}/reversalreceipt — Receipt + storno fields."""

    receipt_number: str = Field(..., alias="receiptNumber")
    receipt_date_time: str = Field(..., alias="receiptDateTime")
    fiscal_memory_serial_number: str = Field(..., alias="fiscalMemorySerialNumber")
    reason: Literal["operator-error", "refund", "tax-base-reduction"] = "refund"


class Invoice(Receipt):
    """Body of POST /printers/{id}/invoice — fiscal invoice (фактура).

    On firmware that supports native invoice (Datecs ISL FW 3.00+),
    these fields are passed verbatim to `open_invoice_receipt`.
    On older firmware, the proxy falls back to a regular fiscal
    receipt prefixed with comment lines containing the same data.
    """

    customer_name: str = Field(..., alias="customerName")
    customer_eik: str = Field(..., alias="customerEik")
    customer_eik_type: Literal["0", "1", "2", "3"] = Field(
        "0", alias="customerEikType",
        description="0=BULSTAT, 1=EGN, 2=Foreign, 3=Service number",
    )
    customer_address: str = Field("", alias="customerAddress")
    customer_buyer: str = Field("", alias="customerBuyer",
                                 description="МОЛ — responsible person")
    customer_vat: str = Field("", alias="customerVat",
                               description="ИН по ЗДДС")
    invoice_number: Optional[str] = Field(
        None, alias="invoiceNumber",
        description="10-digit; omit to let the device auto-increment",
    )


# ─── Cash operation envelopes ────────────────────────────────────────


class TransferAmount(_CamelModel):
    """Body of POST /printers/{id}/withdraw and /deposit."""

    amount: float
    text: Optional[str] = None


class CurrentDateTime(_CamelModel):
    """Body of POST /printers/{id}/datetime."""

    device_date_time: str = Field(..., alias="deviceDateTime")


# ─── Raw request envelope ────────────────────────────────────────────


class RequestFrame(_CamelModel):
    """Body of POST /printers/{id}/rawrequest — raw command bytes."""

    raw_request: str = Field(..., alias="rawRequest")
    # Per ErpNet.FP, this is hex-encoded bytes. We pass through verbatim.


# ─── Response envelopes ──────────────────────────────────────────────


class PrintReceiptResult(_CamelModel):
    """Returned by POST /printers/{id}/receipt and friends."""

    ok: bool = True
    messages: list[StatusMessage] = Field(default_factory=list)
    receipt_number: Optional[str] = Field(None, alias="receiptNumber")
    receipt_date_time: Optional[str] = Field(None, alias="receiptDateTime")
    receipt_amount: Optional[float] = Field(None, alias="receiptAmount")
    fiscal_memory_serial_number: Optional[str] = Field(
        None, alias="fiscalMemorySerialNumber"
    )


class CashAmountResult(_CamelModel):
    """Returned by GET /printers/{id}/cash."""

    ok: bool = True
    messages: list[StatusMessage] = Field(default_factory=list)
    amount: float = 0.0


class TaskInfoResult(_CamelModel):
    """Returned by GET /printers/taskinfo?id=..."""

    task_id: str = Field("", alias="taskId")
    task_status: Literal["unknown", "enqueued", "running", "finished"] = Field(
        "unknown", alias="taskStatus"
    )
    result: Optional[dict] = None


class GenericResult(_CamelModel):
    """Generic ok/messages envelope used by xreport / zreport / duplicate /
    reset / withdraw / deposit / datetime when no body-specific result fields
    apply.
    """

    ok: bool = True
    messages: list[StatusMessage] = Field(default_factory=list)
