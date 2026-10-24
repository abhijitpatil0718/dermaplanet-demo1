/* Runs in <head> before the page paints, so dark mode never flashes white. */
(function () {
  var theme = null;
  try { theme = localStorage.getItem("dp-theme"); } catch (e) {}
  if (theme !== "dark" && theme !== "light") {
    theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  document.documentElement.setAttribute("data-theme", theme);
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "dark" ? "#0c1813" : "#1a7f5c");
})();
