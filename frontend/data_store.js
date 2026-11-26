/**
 * Centralized Data Store for Market Signals V7
 * Eliminates duplicate API calls by caching responses with TTL
 */

const IS_DEV = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';

class DataStore {
    constructor() {
        this.cache = new Map();
        this.pendingRequests = new Map();
        this.subscribers = new Map();

        // Default TTLs for different endpoint types (milliseconds)
        this.defaultTTLs = {
            '/api/news-tab/state': 60000,        // 60s - news updates moderately
            '/api/calendar/state': 120000,       // 120s - calendar changes slowly
            '/api/scanner/state': 30000,         // 30s - scanner updates frequently
            '/api/hotlist/state': 45000,         // 45s - hotlist updates moderately
            '/api/live-prices': 5000,            // 5s - prices update very frequently
            '/api/state': 30000,                 // 30s - general state
            '/api/watchlist': 30000,             // 30s - watchlist auto-refresh per spec
            '/api/config': 300000,               // 300s - config rarely changes
        };
    }

    /**
     * Fetch data from API with caching
     * @param {string} endpoint - API endpoint to fetch
     * @param {number} ttl - Time to live in milliseconds (optional, uses default if not provided)
     * @param {boolean} forceRefresh - Force refresh even if cached data exists
     * @returns {Promise<any>} - API response data
     */
    async fetch(endpoint, ttl = null, forceRefresh = false) {
        // Determine TTL
        const effectiveTTL = ttl || this.defaultTTLs[endpoint] || 30000;

        // Check cache first (unless force refresh)
        if (!forceRefresh) {
            const cached = this.cache.get(endpoint);
            if (cached && Date.now() - cached.timestamp < effectiveTTL) {
                if (IS_DEV) console.log(`[DataStore] Cache HIT for ${endpoint}`);
                return cached.data;
            }
        }

        if (IS_DEV) console.log(`[DataStore] Cache MISS for ${endpoint} - fetching...`);

        // Prevent duplicate concurrent requests
        if (this.pendingRequests.has(endpoint)) {
            if (IS_DEV) console.log(`[DataStore] Request already pending for ${endpoint} - waiting...`);
            return await this.pendingRequests.get(endpoint);
        }

        // Fetch from API
        const promise = fetch(endpoint)
            .then(response => {
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
                }
                return response.json();
            })
            .catch(error => {
                console.error(`[DataStore] Error fetching ${endpoint}:`, error);
                // Return cached data if available, even if stale
                const cached = this.cache.get(endpoint);
                if (cached) {
                    console.warn(`[DataStore] Using stale cache for ${endpoint} due to error`);
                    return cached.data;
                }
                throw error;
            });

        this.pendingRequests.set(endpoint, promise);

        try {
            const data = await promise;

            // Cache the response
            this.cache.set(endpoint, {
                data: data,
                timestamp: Date.now()
            });

            // Notify subscribers
            this.notifySubscribers(endpoint, data);

            if (IS_DEV) console.log(`[DataStore] Cached response for ${endpoint}`);
            return data;
        } finally {
            this.pendingRequests.delete(endpoint);
        }
    }

    /**
     * Invalidate cache for specific endpoint or all endpoints
     * @param {string} endpoint - Endpoint to invalidate (optional, invalidates all if not provided)
     */
    invalidate(endpoint = null) {
        if (endpoint) {
            this.cache.delete(endpoint);
            if (IS_DEV) console.log(`[DataStore] Invalidated cache for ${endpoint}`);
        } else {
            this.cache.clear();
            if (IS_DEV) console.log(`[DataStore] Invalidated ALL cache`);
        }
    }

    /**
     * Subscribe to data changes for an endpoint
     * @param {string} endpoint - Endpoint to subscribe to
     * @param {Function} callback - Callback function to call when data changes
     * @returns {Function} - Unsubscribe function
     */
    subscribe(endpoint, callback) {
        if (!this.subscribers.has(endpoint)) {
            this.subscribers.set(endpoint, new Set());
        }
        this.subscribers.get(endpoint).add(callback);

        // Return unsubscribe function
        return () => {
            const subs = this.subscribers.get(endpoint);
            if (subs) {
                subs.delete(callback);
            }
        };
    }

    /**
     * Notify all subscribers of data changes
     * @param {string} endpoint - Endpoint that changed
     * @param {any} data - New data
     */
    notifySubscribers(endpoint, data) {
        const subs = this.subscribers.get(endpoint);
        if (subs) {
            subs.forEach(callback => {
                try {
                    callback(data);
                } catch (error) {
                    console.error(`[DataStore] Error in subscriber callback for ${endpoint}:`, error);
                }
            });
        }
    }

    /**
     * Get cache statistics
     * @returns {Object} - Cache statistics
     */
    getStats() {
        const stats = {
            totalEntries: this.cache.size,
            pendingRequests: this.pendingRequests.size,
            subscribers: 0,
            entries: []
        };

        this.subscribers.forEach((subs, endpoint) => {
            stats.subscribers += subs.size;
        });

        this.cache.forEach((value, key) => {
            const age = Date.now() - value.timestamp;
            const ttl = this.defaultTTLs[key] || 30000;
            stats.entries.push({
                endpoint: key,
                age: age,
                ttl: ttl,
                fresh: age < ttl
            });
        });

        return stats;
    }

    /**
     * Clear old cache entries (cleanup)
     * @param {number} maxAge - Maximum age in milliseconds (default: 10 minutes)
     */
    cleanup(maxAge = 600000) {
        const now = Date.now();
        let cleaned = 0;

        this.cache.forEach((value, key) => {
            if (now - value.timestamp > maxAge) {
                this.cache.delete(key);
                cleaned++;
            }
        });

        if (cleaned > 0 && IS_DEV) {
            console.log(`[DataStore] Cleaned up ${cleaned} old cache entries`);
        }
    }
}

// Create global singleton instance
window.dataStore = new DataStore();

// Auto-cleanup every 5 minutes
setInterval(() => {
    window.dataStore.cleanup();
}, 300000);