/** Small presentational pieces used by the settings page. */

export function StatusItem({
  ready,
  label,
  value,
}: {
  ready: boolean;
  label: string;
  value: string;
}) {
  return (
    <article className={ready ? "ready" : "missing"}>
      <span className="personal-status-dot" aria-hidden="true" />
      <div>
        <small>{label}</small>
        <strong>{value}</strong>
      </div>
    </article>
  );
}

export function EnvironmentGroup({
  index,
  title,
  variables,
  description,
}: {
  index: string;
  title: string;
  variables: string[];
  description: string;
}) {
  return (
    <article>
      <span className="ai-step-number" aria-hidden="true">
        {index}
      </span>
      <div>
        <h3>{title}</h3>
        <div className="env-variable-list">
          {variables.map((variable) => (
            <code key={variable}>{variable}</code>
          ))}
        </div>
        <p>{description}</p>
      </div>
    </article>
  );
}
