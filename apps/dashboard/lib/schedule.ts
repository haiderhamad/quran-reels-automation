import { TIME_REGEX, type PublishingScheduleEntry } from "./schema";
import cadence from "../../../data/publishing_cadence.json";

export const BERLIN_TIMEZONE = "Europe/Berlin";
export const PRODUCTION_PUBLISHING_TIMES = cadence.slots.map((entry) => entry.time);
export const PRODUCTION_PUBLISHING_SLOTS = cadence.slots.map((entry) => ({
  id: entry.id,
  time: entry.time,
}));

export interface BerlinClock {
  date: string;
  time: string;
}

export interface UpcomingPublishingTime {
  date: string;
  time: string;
  slotId: string;
  dayLabel: "Today" | "Tomorrow" | string;
}

export function sortPublishingSchedule(
  schedule: readonly PublishingScheduleEntry[]
): PublishingScheduleEntry[] {
  return [...schedule].sort((a, b) => a.time.localeCompare(b.time) || a.id.localeCompare(b.id));
}

export function getProductionPublishingSchedule(
  schedule: readonly PublishingScheduleEntry[]
): PublishingScheduleEntry[] {
  // The Admin chooses wall-clock times; IDs remain stable across edits so
  // Supabase scheduler claims still prevent duplicate publication.
  const active = schedule.filter((entry) => entry.enabled);
  const approved = sortPublishingSchedule(active).filter(
    (entry) => cadence.slots.some((slot) => slot.id === entry.id)
  );
  if (
    active.length < 1 || active.length > 3 ||
    approved.length !== active.length ||
    new Set(approved.map((entry) => entry.id)).size !== approved.length ||
    new Set(approved.map((entry) => entry.time)).size !== approved.length ||
    approved.some((entry) => !TIME_REGEX.test(entry.time))
  ) return [];
  return approved;
}

export function assertProductionPublishingSchedule(
  schedule: readonly PublishingScheduleEntry[]
): void {
  const enabledCount = schedule.filter((entry) => entry.enabled).length;
  if (enabledCount < 1 || enabledCount > 3 ||
      getProductionPublishingSchedule(schedule).length !== enabledCount) {
    throw new Error("Choose 1 to 3 different approved publishing times (HH:MM, Europe/Berlin).");
  }
}

export function findDuplicateActiveTime(
  schedule: readonly PublishingScheduleEntry[]
): string | null {
  const seen = new Set<string>();
  for (const entry of schedule) {
    if (!entry.enabled) continue;
    if (seen.has(entry.time)) return entry.time;
    seen.add(entry.time);
  }
  return null;
}

export function getBerlinClock(now = new Date()): BerlinClock {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: BERLIN_TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(now);
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((entry) => entry.type === type)?.value || "";
  return {
    date: `${part("year")}-${part("month")}-${part("day")}`,
    time: `${part("hour")}:${part("minute")}`,
  };
}

export function addCalendarDays(date: string, days: number): string {
  const [year, month, day] = date.split("-").map(Number);
  const next = new Date(Date.UTC(year, month - 1, day + days, 12));
  return next.toISOString().slice(0, 10);
}

export const DEFAULT_GRACE_WINDOW_MINUTES = 45;

export function isSlotDue(
  slotTime: string,
  now = new Date(),
  graceWindowMinutes = DEFAULT_GRACE_WINDOW_MINUTES
): boolean {
  const clock = getBerlinClock(now);
  const [currH, currM] = clock.time.split(":").map(Number);
  const [slotH, slotM] = slotTime.split(":").map(Number);
  const elapsed = (currH * 60 + currM) - (slotH * 60 + slotM);
  return elapsed >= 0 && elapsed <= graceWindowMinutes;
}

export function getDuePublishingSlots(
  schedule: readonly PublishingScheduleEntry[],
  now = new Date(),
  graceWindowMinutes = DEFAULT_GRACE_WINDOW_MINUTES
): PublishingScheduleEntry[] {
  return sortPublishingSchedule(schedule).filter(
    (entry) => entry.enabled && isSlotDue(entry.time, now, graceWindowMinutes)
  );
}

export function getUpcomingPublishingTimes(
  schedule: readonly PublishingScheduleEntry[],
  now = new Date(),
  limit = 4
): UpcomingPublishingTime[] {
  if (limit <= 0) return [];
  const enabled = sortPublishingSchedule(schedule).filter((entry) => entry.enabled);
  if (enabled.length === 0) return [];
  const clock = getBerlinClock(now);
  const upcoming: UpcomingPublishingTime[] = [];

  for (let offset = 0; upcoming.length < limit && offset < 8; offset += 1) {
    const date = addCalendarDays(clock.date, offset);
    for (const entry of enabled) {
      if (offset === 0 && entry.time <= clock.time) continue;
      upcoming.push({
        date,
        time: entry.time,
        slotId: entry.id,
        dayLabel: offset === 0 ? "Today" : offset === 1 ? "Tomorrow" : date,
      });
      if (upcoming.length === limit) break;
    }
  }
  return upcoming;
}
