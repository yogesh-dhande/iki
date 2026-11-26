// For the scripts' tests: the one element a selector names, or a failure saying which.
export function one<T extends Element = HTMLElement>(
  root: ParentNode,
  selector: string,
  // Says which element type it is, so the type parameter isn't only in the result.
  type?: abstract new () => T,
): T {
  const found = root.querySelector<T>(selector);
  if (found && type && !(found instanceof type)) {
    throw new Error(`${selector} isn't a ${type.name}`);
  }
  if (!found) {
    throw new Error(`Nothing matches ${selector}`);
  }
  return found;
}
