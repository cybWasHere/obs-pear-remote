// Rough frame: draws a card's panel and edge as an SVG quad whose corners sit a few px off
// square instead of a CSS rounded rect. Each card gets its own random corners; the quad
// redraws when the card resizes.
// Style it with .frame polygon { fill; stroke } and give the card position: relative.
const RoughFrame = (() => {
  const NS = "http://www.w3.org/2000/svg";

  function attach(node, maxOff = 3) {
    const off = Array.from({ length: 8 }, () => Math.random() * maxOff);
    const svg = document.createElementNS(NS, "svg");
    const poly = document.createElementNS(NS, "polygon");
    svg.setAttribute("class", "frame");
    svg.setAttribute("aria-hidden", "true");
    svg.append(poly);
    node.prepend(svg);

    const draw = () => {
      const w = node.offsetWidth, h = node.offsetHeight;
      if (!w || !h) return;
      const i = 0.75;                      // keep the stroke inside the box
      const [ax, ay, bx, by, cx, cy, dx, dy] = off;
      svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
      poly.setAttribute("points", [
        [i + ax, i + ay], [w - i - bx, i + by], [w - i - cx, h - i - cy], [i + dx, h - i - dy],
      ].map(p => p.map(n => n.toFixed(1)).join(",")).join(" "));
    };
    new ResizeObserver(draw).observe(node);
    draw();
  }

  return { attach };
})();
