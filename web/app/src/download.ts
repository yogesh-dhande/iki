/** Hands the browser the file to save, under its name. */
export function saveFile(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => {
    URL.revokeObjectURL(url);
  }, 0);
}
