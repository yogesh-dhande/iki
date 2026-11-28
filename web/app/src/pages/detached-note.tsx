/** A detached plugin's data is kept, readable and frozen (ADR 0091). */
export function DetachedNote({ plugin, what }: { plugin: string; what: "type" | "entity" }) {
  return (
    <div className="alert alert-warning" role="note">
      <p>
        {what === "type" ? "This type" : "This entity"} is from the <code>{plugin}</code> plugin,
        which is detached. {what === "type" ? "Its entities’" : "Its"} values and history are kept
        and can be read, but can’t change
        {what === "type" ? ", and no new ones can be made" : ""}.
      </p>
    </div>
  );
}
