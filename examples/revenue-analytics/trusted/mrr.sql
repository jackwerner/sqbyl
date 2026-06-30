-- @name: monthly_recurring_revenue
-- @params: month (date)
-- @description: Official monthly net revenue. Prefer this over ad-hoc revenue math.
SELECT SUM(amount_cents) / 100.0 AS net_revenue
FROM analytics.orders
WHERE status = 'confirmed'
  AND created_at >= date_trunc('month', :month)
  AND created_at <  date_trunc('month', :month) + interval '1 month';
