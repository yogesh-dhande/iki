/** A preview's zoom: fit to the frame's width, or a share of the image's own pixels. */

export type Zoom = "fit" | number;

export const ZOOMS: readonly number[] = [0.125, 0.25, 0.5, 1, 2, 4, 8];

export function zoomLabel(zoom: Zoom): string {
  return zoom === "fit" ? "Fit" : `${String(Math.round(zoom * 1000) / 10)}%`;
}

export function ZoomControl({
  id,
  zoom,
  onZoom,
  zooms = ZOOMS,
}: {
  id: string;
  zoom: Zoom;
  onZoom: (zoom: Zoom) => void;
  zooms?: readonly number[];
}) {
  return (
    <span className="preview-zoom">
      <label htmlFor={`${id}-zoom`}>Zoom</label>
      <select
        id={`${id}-zoom`}
        value={String(zoom)}
        onChange={(event) => {
          const value = event.target.value;
          onZoom(value === "fit" ? "fit" : Number(value));
        }}
      >
        <option value="fit">Fit width</option>
        {zooms.map((z) => (
          <option key={z} value={String(z)}>
            {zoomLabel(z)}
          </option>
        ))}
      </select>
    </span>
  );
}
