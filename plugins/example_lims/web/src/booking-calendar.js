// The frame's bridge sets place and entities on this element (ADR 0117).
import { more, open, refine } from "@ikisci/component";
import { BookingCalendar } from "@ikisci/science/booking-calendar";

class InstrumentBookings extends BookingCalendar {
  #label = "Bookings";
  #asked = false;

  constructor() {
    super();
    this.addEventListener("month-change", (event) => this.#request(event.detail));
    this.addEventListener("booking-open", (event) => open(this, event.detail.id));
  }

  #request({ start, end }) {
    refine(this, {
      where: {
        all: [
          { field: "starts_at", op: "gte", value: start },
          { field: "starts_at", op: "lt", value: end },
        ],
      },
      sort: { field: "starts_at", descending: false },
    });
  }

  set place(place) {
    this.#label = place.label;
    this.data = { ...this.data, label: this.#label };
    if (!this.#asked) {
      this.#asked = true;
      this.#request(this.range);
    }
  }

  set entities({ items, loading, limited, error, more: hasMore }) {
    this.data = {
      label: this.#label,
      bookings: items.map(({ hrid, title, refs, values }) => ({
        id: hrid,
        title: values.purpose ?? title,
        startsAt: values.starts_at,
        endsAt: values.ends_at,
        instrument: refs[values.instrument]?.title,
      })),
      loading: loading || hasMore,
      limited,
      error,
    };
    if (hasMore && !loading) more(this);
  }
}

customElements.define("lims-booking-calendar", InstrumentBookings);
