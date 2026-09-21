import { formatDate, type NotificationView } from "../lib/dashboard";

/**
 * Unread operational alerts (worker failures, budget exhaustion, quarantined
 * trades). Presentational only: the page owns fetching and acknowledgement.
 */
export default function NotificationsPanel({
  notifications,
  onMarkRead,
}: {
  notifications: NotificationView[];
  onMarkRead: (id: number) => void;
}) {
  const unread = notifications.filter((item) => !item.read);
  if (unread.length === 0) return null;
  return (
    <section className="panel notification-panel" aria-label="系统提醒">
      <div className="section-heading">
        <div>
          <p className="eyebrow">ATTENTION</p>
          <h2>需要关注</h2>
        </div>
        <span className="pill degraded">{unread.length} 条未读</span>
      </div>
      <div className="notification-list">
        {unread.map((item) => (
          <article className={`notification-item ${item.severity}`} key={item.id}>
            <div>
              <strong>{item.title}</strong>
              <p>{item.message}</p>
              <small>{formatDate(item.createdAt)}</small>
            </div>
            <button
              type="button"
              className="text-button"
              onClick={() => onMarkRead(item.id)}
            >
              已处理
            </button>
          </article>
        ))}
      </div>
    </section>
  );
}
