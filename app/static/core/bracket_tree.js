document.addEventListener("DOMContentLoaded", function () {
    const canvas = document.getElementById("bracket-canvas");
    const svg = document.getElementById("bracket-lines");
    if (!canvas || !svg) {
        return;
    }

    const SVG_NS = "http://www.w3.org/2000/svg";

    function elbow(x1, y1, x2, y2) {
        // Codo clásico de llave: tramo horizontal, vertical y horizontal.
        const midX = x1 + (x2 - x1) / 2;
        return "M " + x1 + " " + y1 + " H " + midX + " V " + y2 + " H " + x2;
    }

    function draw() {
        const canvasRect = canvas.getBoundingClientRect();
        while (svg.firstChild) {
            svg.removeChild(svg.firstChild);
        }
        canvas.querySelectorAll(".tree-match[data-next]").forEach(function (box) {
            const target = document.getElementById(box.dataset.next);
            if (!target) {
                return;
            }
            const source = box.getBoundingClientRect();
            const destiny = target.getBoundingClientRect();
            const path = document.createElementNS(SVG_NS, "path");
            path.setAttribute("class", "bracket-connector");
            path.setAttribute(
                "d",
                elbow(
                    source.right - canvasRect.left,
                    source.top + source.height / 2 - canvasRect.top,
                    destiny.left - canvasRect.left,
                    destiny.top + destiny.height / 2 - canvasRect.top
                )
            );
            svg.appendChild(path);
        });
    }

    draw();
    window.addEventListener("resize", draw);
    window.addEventListener("load", draw);
    if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(draw);
    }
});
