/**
 * API Stats Widget - Real-time monitoring of FMP and Yahoo API requests
 */

class APIStatsWidget {
    constructor() {
        this.combinedHistory = [];
        this.maxDataPoints = 36; // 3 minutes at 5s interval
        this.pollInterval = null;
        this.inFlight = false;
    }

    hasTargets() {
        return !!(
            document.getElementById('api-fmp-rpm')
            || document.getElementById('api-yahoo-rpm')
            || document.getElementById('api-sparkline')
        );
    }

    start() {
        if (this.pollInterval || !this.hasTargets()) return;
        this.fetchStats(); // Initial fetch
        this.pollInterval = setInterval(() => this.fetchStats(), 20000);
    }

    stop() {
        if (this.pollInterval) {
            clearInterval(this.pollInterval);
            this.pollInterval = null;
        }
    }

    async fetchStats() {
        if (!this.hasTargets()) {
            this.stop();
            return;
        }
        if (this.inFlight) return;
        this.inFlight = true;
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 8000);
        try {
            const response = await fetch('/api/stats/api-requests', { signal: controller.signal });
            if (!response.ok) {
                return;
            }
            const data = await response.json();

            const current = data.current || { fmp_rpm: 0, yahoo_rpm: 0 };

            // Update displays
            const fmpEl = document.getElementById('api-fmp-rpm');
            const yfEl = document.getElementById('api-yahoo-rpm');
            if (fmpEl) fmpEl.textContent = current.fmp_rpm;
            if (yfEl) yfEl.textContent = current.yahoo_rpm;

            // Update combined history (FMP + Yahoo)
            const combined = current.fmp_rpm + current.yahoo_rpm;
            this.combinedHistory.push(combined);

            // Keep only last N data points
            if (this.combinedHistory.length > this.maxDataPoints) {
                this.combinedHistory.shift();
            }

            // Show/hide canvas based on data availability
            const canvas = document.getElementById('api-sparkline');
            if (canvas) {
                if (this.combinedHistory.length >= 2) {
                    canvas.style.display = 'block';
                    this.renderSparkline('api-sparkline', this.combinedHistory, '#475569');
                } else {
                    canvas.style.display = 'none';
                }
            }

        } catch (error) {
            // Non-critical widget: fail silently when the stats endpoint is unavailable.
        } finally {
            clearTimeout(timeoutId);
            this.inFlight = false;
        }
    }

    renderSparkline(canvasId, data, color) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;

        const ctx = canvas.getContext('2d');
        const width = canvas.width;
        const height = canvas.height;

        // Clear canvas
        ctx.clearRect(0, 0, width, height);

        if (data.length < 2) return;

        // Find min/max for scaling
        const max = Math.max(...data, 1);
        const min = 0;
        const range = max - min || 1;

        // Draw line
        ctx.beginPath();
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.2;
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';

        data.forEach((value, index) => {
            const x = (index / (data.length - 1)) * width;
            const y = height - ((value - min) / range) * (height - 2) - 1;

            if (index === 0) {
                ctx.moveTo(x, y);
            } else {
                ctx.lineTo(x, y);
            }
        });

        ctx.stroke();

        // Add subtle area fill
        ctx.lineTo(width, height);
        ctx.lineTo(0, height);
        ctx.closePath();

        const gradient = ctx.createLinearGradient(0, 0, 0, height);
        gradient.addColorStop(0, 'rgba(71, 85, 105, 0.15)');
        gradient.addColorStop(1, 'rgba(71, 85, 105, 0)');
        ctx.fillStyle = gradient;
        ctx.fill();
    }
}


// Initialize and auto-start when DOM is ready
window.apiStatsWidget = new APIStatsWidget();

// Auto-start polling on page load
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', async () => {
        await (window.MSAuthReady || Promise.resolve());
        window.apiStatsWidget.start();
    });
} else {
    // DOM already loaded
    (async () => {
        await (window.MSAuthReady || Promise.resolve());
        window.apiStatsWidget.start();
    })();
}
