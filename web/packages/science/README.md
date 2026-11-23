# @ikisci/science

Scientific custom elements and functions for web applications. Import the subpath you
use. Each element takes JavaScript data and dispatches DOM events. All public entries
include TypeScript declarations. The booking calendar can run on a static file server
without an Iki project or account.

## Booking calendar

Import `@ikisci/science/booking-calendar` and create `<iki-booking-calendar>`.
Set `.month` to a local calendar month (`YYYY-MM`). Assign `.data` whenever the
bookings or their loading state change. Each booking supplies an opaque string ID,
title and ISO timestamp with time zone. It may also include an end timestamp and an
instrument label. The example is a fictitious reservation.

```js
import "@ikisci/science/booking-calendar";

const calendar = document.createElement("iki-booking-calendar");
calendar.month = "2026-03";
calendar.data = {
  label: "Instrument room",
  bookings: [
    {
      id: "demo-1",
      title: "Instrument check",
      instrument: "Plate reader",
      startsAt: "2026-03-09T10:00:00Z",
      endsAt: "2026-03-09T11:00:00Z",
    },
  ],
};
calendar.addEventListener("booking-open", (event) => console.log(event.detail.id));
document.body.append(calendar);
```

Use the interval in a `month-change` event to load a different month. The end
is exclusive. See the [data and event contract](stories/booking-calendar.md)
and [runnable story](stories/booking-calendar.html).

## Next.js

Create the element inside a Client Component. Import the browser module in an
effect so custom-element registration runs after mount. Types can be imported
with `import type` on either side. [Next.js Client Components](https://nextjs.org/docs/app/getting-started/server-and-client-components)

```tsx
"use client";

import { useEffect, useRef } from "react";

export default function Calendar() {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = host.current;
    let active = true;
    void import("@ikisci/science/booking-calendar").then(() => {
      if (!node || !active) return;
      const calendar = document.createElement("iki-booking-calendar");
      calendar.data = { bookings: [] };
      node.replaceChildren(calendar);
    });
    return () => {
      active = false;
      node?.replaceChildren();
    };
  }, []);

  return <div ref={host} />;
}
```

## Run the story

From the repository root run `pnpm install --frozen-lockfile`, then
`pnpm --filter @ikisci/science build`. Serve `web/packages` with
`python3 -m http.server --directory web/packages 8000` and open
`/science/stories/booking-calendar.html`. The package build checks each public entry
against its gzip byte limit. The release workflow sets both package versions from the
Iki release tag.
