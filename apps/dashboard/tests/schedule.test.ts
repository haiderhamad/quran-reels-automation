import { describe, expect, it } from "vitest";
import {
  findDuplicateActiveTime,
  getBerlinClock,
  getDuePublishingSlots,
  getUpcomingPublishingTimes,
  sortPublishingSchedule,
} from "../lib/schedule";

const schedule = [
  { id: "night", time: "21:00", enabled: true },
  { id: "morning", time: "08:00", enabled: true },
  { id: "noon", time: "13:00", enabled: false },
  { id: "afternoon", time: "16:00", enabled: true },
];

describe("dynamic Europe/Berlin publishing schedule", () => {
  it("sorts one, two, and four stable slots chronologically without changing IDs", () => {
    expect(sortPublishingSchedule(schedule).map((slot) => slot.id)).toEqual(["morning", "noon", "afternoon", "night"]);
    expect(sortPublishingSchedule(schedule.slice(0, 1))).toHaveLength(1);
    expect(sortPublishingSchedule(schedule.slice(0, 2))).toHaveLength(2);
  });

  it("supports eight slots and preserves stable IDs across edit, disable, and remove operations", () => {
    const eight = Array.from({ length: 8 }, (_, index) => ({
      id: `stable-${index}`,
      time: `${String(7 + index).padStart(2, "0")}:00`,
      enabled: true,
    }));
    const edited = eight.map((slot) => slot.id === "stable-0" ? { ...slot, time: "07:30" } : slot);
    const disabled = edited.map((slot) => slot.id === "stable-3" ? { ...slot, enabled: false } : slot);
    const removed = disabled.filter((slot) => slot.id !== "stable-7");
    expect(sortPublishingSchedule(eight)).toHaveLength(8);
    expect(edited[0]).toMatchObject({ id: "stable-0", time: "07:30" });
    expect(disabled.find((slot) => slot.id === "stable-3")?.enabled).toBe(false);
    expect(removed).toHaveLength(7);
    expect(removed.map((slot) => slot.id)).not.toContain("stable-7");
  });

  it("detects duplicate active times but permits a disabled duplicate", () => {
    expect(findDuplicateActiveTime([{ id: "a", time: "08:00", enabled: true }, { id: "b", time: "08:00", enabled: true }])).toBe("08:00");
    expect(findDuplicateActiveTime([{ id: "a", time: "08:00", enabled: true }, { id: "b", time: "08:00", enabled: false }])).toBeNull();
  });

  it("supports two administrator-selected times without a workflow cron edit", () => {
    const custom = [
      { id: "morning", time: "10:25", enabled: true },
      { id: "evening", time: "18:40", enabled: true },
    ];
    // Summer: Berlin 10:33 / 18:43. Neither time is a GitHub cron posting time.
    expect(getDuePublishingSlots(custom, new Date("2026-08-24T08:33:00Z")).map((slot) => slot.id)).toEqual(["morning"]);
    expect(getDuePublishingSlots(custom, new Date("2026-08-24T16:43:00Z")).map((slot) => slot.id)).toEqual(["evening"]);
    expect(getDuePublishingSlots(custom, new Date("2026-08-24T08:23:00Z"))).toEqual([]);
  });

  it("finds due and not-due entries using Berlin local time and bounded grace window", () => {
    // At 08:20 Berlin (06:20 UTC), morning (08:00) is within 45m grace window
    const nowDue = new Date("2026-08-24T06:20:00Z");
    expect(getDuePublishingSlots(schedule, nowDue).map((slot) => slot.id)).toEqual(["morning"]);

    // At 14:30 Berlin (12:30 UTC), morning (08:00) is outside 45m grace window
    const nowPast = new Date("2026-08-24T12:30:00Z");
    expect(getDuePublishingSlots(schedule, nowPast)).toEqual([]);
  });

  it("calculates the next schedule across today and tomorrow", () => {
    const upcoming = getUpcomingPublishingTimes(schedule, new Date("2026-08-24T12:30:00Z"), 4);
    expect(upcoming.map((entry) => `${entry.dayLabel} ${entry.time}`)).toEqual([
      "Today 16:00",
      "Today 21:00",
      "Tomorrow 08:00",
      "Tomorrow 16:00",
    ]);
  });

  it("is DST-safe across the Berlin spring-forward and repeated autumn hour", () => {
    expect(getBerlinClock(new Date("2026-03-29T00:30:00Z")).time).toBe("01:30");
    expect(getBerlinClock(new Date("2026-03-29T01:30:00Z")).time).toBe("03:30");
    expect(getBerlinClock(new Date("2026-10-25T00:45:00Z")).time).toBe("02:45");
    expect(getBerlinClock(new Date("2026-10-25T01:45:00Z")).time).toBe("02:45");
  });
});
