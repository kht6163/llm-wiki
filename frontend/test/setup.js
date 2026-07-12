import { afterEach } from "vitest";

afterEach(() => {
  document.body.replaceChildren();
  document.documentElement.removeAttribute("data-theme");
});
