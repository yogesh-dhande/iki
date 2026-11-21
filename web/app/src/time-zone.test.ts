import { afterEach, describe, expect, it, vi } from "vitest";

import { rememberTimeZone, TIME_ZONE_COOKIE } from "./time-zone";

function zoneIs(timeZone: string) {
  const options = new Intl.DateTimeFormat().resolvedOptions();
  vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
    ...options,
    timeZone,
  });
}

function cookie(): string | undefined {
  return document.cookie
    .split("; ")
    .find((c) => c.startsWith(`${TIME_ZONE_COOKIE}=`))
    ?.slice(TIME_ZONE_COOKIE.length + 1);
}

describe("the viewer's time zone", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    document.cookie = `${TIME_ZONE_COOKIE}=; path=/; max-age=0`;
  });

  it("is remembered for the pages the server renders", () => {
    zoneIs("America/Los_Angeles");
    rememberTimeZone();
    expect(cookie()).toBe("America/Los_Angeles");
    zoneIs("Asia/Kolkata");
    rememberTimeZone();
    expect(cookie()).toBe("Asia/Kolkata");
  });
});
