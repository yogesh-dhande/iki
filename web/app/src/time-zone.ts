/**
 * Pages the server renders (the audit log, say) show times in the viewer's time zone,
 * as the app does: the app tells the server which one in this cookie
 * (iki_server.web.timezones), and the server's own pages' iki.js does too.
 */
export const TIME_ZONE_COOKIE = "iki_time_zone";

export function rememberTimeZone(): void {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (!zone || !/^[A-Za-z0-9_+\-/]+$/.test(zone)) return;
  const secure = window.location.protocol === "https:" ? "; secure" : "";
  document.cookie = `${TIME_ZONE_COOKIE}=${zone}; path=/; max-age=31536000; samesite=lax${secure}`;
}
