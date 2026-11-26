// The frame's bridge sets place and entities on this element (ADR 0117).
import { more, open, refine } from "@ikisci/component";
import { BookingCalendar } from "@ikisci/science/booking-calendar";

/** What a booking is for, as its reference shows it: by HRID, and its title if it has one. */
function named(ref) {
  if (!ref) return undefined;
  return ref.title && ref.title !== ref.hrid ? `${ref.hrid} ${ref.title}` : ref.hrid;
}

class InstrumentBookings extends BookingCalendar {
  #label = "Bookings";
  #asked = false;
  /** Whether each booking says whom it's for: not on its reader's home, where all are theirs. */
  #naming = true;
  #given = { items: [], loading: true, limited: false, error: null, more: false };
  /** Cancelled bookings hold nothing, so a calendar leaves them out until asked. */
  #cancelled = false;

  constructor() {
    super();
    this.addEventListener("month-change", (event) => this.#request(event.detail));
    this.addEventListener("cancelled-change", (event) => {
      this.#cancelled = event.detail.shown;
      this.#redraw();
      this.#request(this.range);
    });
    this.addEventListener("booking-open", (event) => open(this, event.detail.id));
  }

  /** Whether this place's bookings say whom each is for. */
  names(place) {
    return place.slot !== "person.home";
  }

  #request({ start, end }) {
    refine(this, {
      where: {
        all: [
          { field: "starts_at", op: "gte", value: start },
          { field: "starts_at", op: "lt", value: end },
          ...(this.#cancelled ? [] : [{ field: "cancelled", op: "eq", value: false }]),
        ],
      },
      sort: { field: "starts_at", descending: false },
    });
  }

  set place(place) {
    this.#label = place.label;
    this.#naming = this.names(place);
    this.#redraw();
    if (!this.#asked) {
      this.#asked = true;
      this.#request(this.range);
    }
  }

  set entities(given) {
    this.#given = given;
    this.#redraw();
    if (given.more && !given.loading) more(this);
  }

  #redraw() {
    const { items, loading, limited, error, more: hasMore } = this.#given;
    this.data = {
      label: this.#label,
      bookings: items.map(({ hrid, title, refs, people, values }) => ({
        id: hrid,
        title: values.purpose ?? title,
        startsAt: values.starts_at,
        endsAt: values.ends_at,
        instrument: refs[values.instrument]?.title,
        bookedFor: this.#naming ? people?.[values.booked_by]?.name : undefined,
        // Only what its reader can read is in refs: any other record isn't named.
        work: named(refs[values.work]),
        cancelled: values.cancelled === true,
      })),
      showCancelled: this.#cancelled,
      loading: loading || hasMore,
      limited,
      error,
    };
  }
}

/** The bookings its reader made for other people: each says whom it's for. */
class BookingsMade extends InstrumentBookings {
  names() {
    return true;
  }
}

customElements.define("equipment-booking-calendar", InstrumentBookings);
customElements.define("equipment-bookings-made", BookingsMade);
