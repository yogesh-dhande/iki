# Booking calendar

Import `@ikisci/science/booking-calendar` in the browser to register
`<iki-booking-calendar>`. The calendar shows one month of bookings in the viewer’s
locale and time zone. Weeks start on Monday. A booking spanning midnight appears
on the day it starts.

Assign a new `data` object to update the calendar. The `bookings` array can hold
one month or the whole schedule. The element preserves the order of entries within
each day.

| Property of `data` | Type                    | Meaning                                          |
| ------------------ | ----------------------- | ------------------------------------------------ |
| `bookings`         | `readonly Booking[]`    | The entries available to display                 |
| `label`            | optional string         | Table caption, defaults to “Bookings”            |
| `loading`          | optional boolean        | More bookings are being loaded                   |
| `limited`          | optional boolean        | The supplied collection reached its host’s limit |
| `error`            | optional string or null | Message to display in the status region          |

Each booking has a string `id`, a string `title` and an ISO date-time string
`startsAt`. Optional strings `endsAt` and `instrument` add an end time and a
resource name. Use an explicit offset or `Z` for an absolute time. A date-time
with no offset is local to the viewer. A booking with an invalid start time is
skipped. An invalid or absent end time displays only the start time.

The `month` property is a local calendar month in `YYYY-MM` format and defaults
to the current month. Its setter rejects invalid formats with a `RangeError`.
The read-only `range` property gives the month’s inclusive start and exclusive
end as ISO instants.

| DOM event      | `event.detail`                   | When it fires                           |
| -------------- | -------------------------------- | --------------------------------------- |
| `booking-open` | `{ id: string }`                 | A booking is activated                  |
| `month-change` | `{ start: string, end: string }` | A person uses a month navigation button |

Both events bubble and cross shadow boundaries. Setting `month` from code updates
the display silently. Hosts that page by month can read `range` for the initial
load and listen for `month-change` to load subsequent months. The entry exports
the element class `BookingCalendar` and the types `Booking`, `BookingCalendarData`,
`BookingMonth` and `BookingOpen`.

[Run the instrument booking example](booking-calendar.html).
