(() => {
    const canvas = document.querySelector("#ratingChart");
    if (!canvas) return;

    const values = JSON.parse(canvas.dataset.values);
    const context = canvas.getContext("2d");
    if (!context) return;

    function drawChart() {
        const bounds = canvas.getBoundingClientRect();
        if (!bounds.width || !bounds.height) return;

        const pixelRatio = window.devicePixelRatio || 1;
        canvas.width = Math.round(bounds.width * pixelRatio);
        canvas.height = Math.round(bounds.height * pixelRatio);
        context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
        context.clearRect(0, 0, bounds.width, bounds.height);

        const padding = { top: 12, right: 12, bottom: 30, left: 34 };
        const chartWidth = bounds.width - padding.left - padding.right;
        const chartHeight = bounds.height - padding.top - padding.bottom;
        const largestValue = Math.max(5, ...values);
        const stepValue = Math.max(1, Math.ceil(largestValue / 4));
        const maximum = stepValue * 4;

        context.font = "12px Arial, sans-serif";
        context.textAlign = "right";
        context.textBaseline = "middle";
        context.strokeStyle = "#e2e8f0";
        context.fillStyle = "#64748b";
        context.lineWidth = 1;

        for (let line = 0; line <= 4; line += 1) {
            const y = padding.top + chartHeight * line / 4;
            const label = String(maximum - stepValue * line);
            context.beginPath();
            context.moveTo(padding.left, y);
            context.lineTo(bounds.width - padding.right, y);
            context.stroke();
            context.fillText(label, padding.left - 8, y);
        }

        const slotWidth = chartWidth / values.length;
        const barWidth = slotWidth * 0.48;
        values.forEach((value, index) => {
            const barHeight = chartHeight * value / maximum;
            const x = padding.left + slotWidth * index + (slotWidth - barWidth) / 2;
            const y = padding.top + chartHeight - barHeight;
            context.fillStyle = "#2563eb";
            context.beginPath();
            context.roundRect(x, y, barWidth, barHeight, [5, 5, 0, 0]);
            context.fill();
            context.fillStyle = "#64748b";
            context.textAlign = "center";
            context.textBaseline = "top";
            context.fillText(`${index + 1} star${index === 0 ? "" : "s"}`, x + barWidth / 2, bounds.height - padding.bottom + 9);
        });
    }

    drawChart();
    if ("ResizeObserver" in window) {
        new ResizeObserver(drawChart).observe(canvas);
    } else {
        window.addEventListener("resize", drawChart);
    }
})();
