from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, func, or_, select

from .booking import HOTEL_ZONE
from .models import Booking, BookingPriceLine, Room, RoomBlock, RoomNight, RoomType


DAY = timedelta(days=1)


@dataclass(frozen=True)
class RoomTypeStats:
    name: str
    sellable: int
    occupied: int
    reserved: int
    confirmed_value: Decimal

    @property
    def occupancy(self):
        if self.sellable:
            return Decimal(self.occupied * 100) / self.sellable
        return None

    @property
    def average_price(self):
        return self.confirmed_value / self.occupied if self.occupied else None


@dataclass(frozen=True)
class OccupancyReport:
    start: date
    end: date
    rows: list[RoomTypeStats]
    total: RoomTypeStats
    trend: list[tuple[date, Decimal | None]]


def report_range(period, today, values):
    try:
        if period == "custom":
            start = date.fromisoformat(values.get("start", ""))
            end = date.fromisoformat(values.get("end", "")) + DAY
        else:
            anchor = date.fromisoformat(values["anchor"]) if values.get("anchor") else today
            if period == "week":
                start = anchor - timedelta(days=anchor.weekday())
                end = start + timedelta(days=7)
            elif period in {"month", "quarter"}:
                months = 3 if period == "quarter" else 1
                start_month = (anchor.month - 1) // months * months + 1
                start = date(anchor.year, start_month, 1)
                next_year, next_month = divmod(start_month - 1 + months, 12)
                end = date(anchor.year + next_year, next_month + 1, 1)
            else:
                raise ValueError("Unknown period")
        if end <= start:
            raise ValueError("Invalid date range")
    except (ValueError, OverflowError) as exc:
        raise ValueError("Enter a valid report period and date range.") from exc
    return start, end


def business_date(timestamp):
    return timestamp.replace(tzinfo=timezone.utc).astimezone(HOTEL_ZONE).date()


def merged_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def occupancy_report(session, start, end, today, *, daily=False):
    past_end = min(end, today)
    rooms = {room.id: room for room in session.scalars(select(Room))}
    room_types = session.execute(select(RoomType.id, RoomType.name).order_by(RoomType.name, RoomType.id))
    names = dict(room_types.all())
    occupied_by_room = defaultdict(set)
    occupied = defaultdict(int)
    booked_value = defaultdict(lambda: Decimal("0.00"))
    sellable = defaultdict(int)
    blocks = defaultdict(list)
    daily_occupied = defaultdict(int)
    daily_sellable = defaultdict(int)
    chart_days = [start + offset * DAY for offset in range((past_end - start).days)] if daily else []

    if start < past_end:
        for room_id, begin, finish in session.execute(select(RoomBlock.room_id, RoomBlock.start_date, RoomBlock.end_date).where(
            RoomBlock.effective.is_(True), RoomBlock.start_date < past_end, RoomBlock.end_date > start,
        )):
            blocks[room_id].append((begin, finish))

        nights = session.execute(
            select(
                RoomNight.room_id, RoomNight.stay_date, Booking.room_type_id, Booking.checked_in_at,
                Booking.checked_out_at, BookingPriceLine.amount,
            ).join(Booking, Booking.id == RoomNight.booking_id).outerjoin(
                BookingPriceLine, and_(
                    BookingPriceLine.booking_id == Booking.id, BookingPriceLine.stay_date == RoomNight.stay_date,
                ),
            ).where(
                RoomNight.stay_date >= start, RoomNight.stay_date < past_end,
                Booking.status.in_(("checked_in", "checked_out")), Booking.checked_in_at.is_not(None),
            )
        )
        for room_id, night, type_id, arrived, departed, amount in nights:
            if night >= business_date(arrived) and (departed is None or night < business_date(departed)):
                occupied_by_room[room_id].add(night)
                occupied[type_id] += 1
                booked_value[type_id] += amount or Decimal("0.00")
                if daily:
                    daily_occupied[night] += 1

        for room_id, room in rooms.items():
            begins = max(start, room.operational_from)
            finishes = min(past_end, room.operational_until or past_end)
            if begins >= finishes:
                continue
            overlaps = merged_ranges(
                (max(begins, first), min(finishes, last))
                for first, last in blocks[room_id] if first < finishes and last > begins
            )
            blocked = sum(
                (last - first).days - sum(first <= night < last for night in occupied_by_room[room_id])
                for first, last in overlaps
            )
            sellable[room.room_type_id] += (finishes - begins).days - blocked
            for night in chart_days:
                if begins <= night < finishes and (
                    night in occupied_by_room[room_id] or not any(first <= night < last for first, last in overlaps)
                ):
                    daily_sellable[night] += 1

    reserved = dict(session.execute(
        select(Booking.room_type_id, func.count(RoomNight.id))
        .join(Booking, Booking.id == RoomNight.booking_id)
        .where(
            RoomNight.stay_date >= max(today, start), RoomNight.stay_date < end,
            Booking.status.in_(("confirmed", "checked_in")),
        ).group_by(Booking.room_type_id)
    ).all())
    rows = [
        RoomTypeStats(name, sellable[type_id], occupied[type_id], reserved.get(type_id, 0), booked_value[type_id])
        for type_id, name in names.items()
    ]
    total = RoomTypeStats(
        "All room types", sum(row.sellable for row in rows), sum(row.occupied for row in rows),
        sum(row.reserved for row in rows), sum((row.confirmed_value for row in rows), Decimal("0.00")),
    )
    trend = [
        (night, Decimal(daily_occupied[night] * 100) / daily_sellable[night] if daily_sellable[night] else None)
        for night in chart_days
    ]
    return OccupancyReport(start, end, rows, total, trend)
