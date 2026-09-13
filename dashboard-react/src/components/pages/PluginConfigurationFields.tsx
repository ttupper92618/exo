import { useId } from 'react';
import styled from 'styled-components';
import { useSkulkTranslation } from '../../i18n/tolgee';

const Fields = styled.div`display: grid; gap: 14px;`;
const Field = styled.label`display: grid; gap: 5px; font-size: ${({ theme }) => theme.fontSizes.sm};`;
const Input = styled.input`
  padding: 9px; border-radius: ${({ theme }) => theme.radii.sm};
  border: 1px solid ${({ theme }) => theme.colors.border};
  background: ${({ theme }) => theme.colors.surface}; color: ${({ theme }) => theme.colors.text};
`;
const Select = styled.select`
  padding: 9px; border-radius: ${({ theme }) => theme.radii.sm};
  border: 1px solid ${({ theme }) => theme.colors.border};
  background: ${({ theme }) => theme.colors.surface}; color: ${({ theme }) => theme.colors.text};
`;

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function kind(schema: Record<string, unknown>): unknown {
  return Array.isArray(schema.type) ? schema.type.find((value) => value !== 'null') : schema.type;
}

/** Resolve only local schema references; schema content never causes a network request. */
function resolve(schema: Record<string, unknown>, root: Record<string, unknown>): Record<string, unknown> {
  if (typeof schema.$ref !== 'string') return schema;
  if (!schema.$ref.startsWith('#/')) return {};
  let current: unknown = root;
  for (const key of schema.$ref.slice(2).split('/')) current = record(current)[key.replaceAll('~1', '/').replaceAll('~0', '~')];
  return { ...record(current), ...Object.fromEntries(Object.entries(schema).filter(([key]) => key !== '$ref')) };
}

/** Refuse unsupported contracts instead of silently dropping settings constraints. */
export function supportedConfigurationSchema(schema: Record<string, unknown>, root = schema, depth = 0): boolean {
  if (depth > 8) return false;
  const resolved = resolve(schema, root);
  if (resolved.writeOnly === true || resolved.format === 'password') return false;
  if (['oneOf', 'anyOf', 'allOf', 'if', 'patternProperties'].some((key) => key in resolved)) return false;
  const type = kind(resolved);
  if (type === 'object') return Object.values(record(resolved.properties)).every((field) => supportedConfigurationSchema(record(field), root, depth + 1));
  return ['string', 'boolean', 'number', 'integer'].includes(String(type));
}

/** Props shared by recursive ordinary-setting controls; secrets are excluded. */
interface ConfigurationFieldsProps {
  schema: Record<string, unknown>;
  rootSchema?: Record<string, unknown>;
  values: Record<string, unknown>;
  onChange: (values: Record<string, unknown>) => void;
  disabled?: boolean;
}

/** Render plugin-declared scalar and nested-object controls without provider code. */
export function PluginConfigurationFields({ schema, rootSchema = schema, values, onChange, disabled }: ConfigurationFieldsProps) {
  const id = useId();
  const { t } = useSkulkTranslation();
  const resolved = resolve(schema, rootSchema);
  const required = Array.isArray(resolved.required) ? resolved.required : [];
  return <Fields>{Object.entries(record(resolved.properties)).map(([name, raw]) => {
    const field = resolve(record(raw), rootSchema);
    const type = kind(field);
    const label = typeof field.title === 'string' ? field.title : name;
    const fieldId = `${id}-${name}`;
    const value = values[name];
    const update = (next: unknown) => {
      const copy = { ...values };
      if (next === undefined) delete copy[name]; else Object.defineProperty(copy, name, { value: next, writable: true, enumerable: true, configurable: true });
      onChange(copy);
    };
    if (type === 'object') return <fieldset key={name} disabled={disabled}><legend>{label}</legend>
      <PluginConfigurationFields schema={field} rootSchema={rootSchema} values={record(value)} onChange={update} disabled={disabled} />
    </fieldset>;
    return <Field key={name} htmlFor={fieldId}>
      <span>{label}{required.includes(name) ? ' *' : ''}</span>
      {Array.isArray(field.enum) ? <Select id={fieldId} value={value === undefined ? '' : JSON.stringify(value)} disabled={disabled} required={required.includes(name)} onChange={(event) => update(event.target.value === '' ? undefined : JSON.parse(event.target.value))}>
        <option value="">{t('plugins.unset', 'Not set')}</option>
        {field.enum.map((option) => <option key={JSON.stringify(option)} value={JSON.stringify(option)}>{String(option)}</option>)}
      </Select> : type === 'boolean' ? <Select id={fieldId} value={value === undefined ? '' : String(value)} disabled={disabled} required={required.includes(name)} onChange={(event) => update(event.target.value === '' ? undefined : event.target.value === 'true')}>
        <option value="">{t('plugins.unset', 'Not set')}</option><option value="true">{t('plugins.yes', 'Yes')}</option><option value="false">{t('plugins.no', 'No')}</option>
      </Select> : <Input id={fieldId} type={type === 'string' ? 'text' : 'number'} value={typeof value === 'string' || typeof value === 'number' ? value : ''} disabled={disabled}
        required={required.includes(name)} step={type === 'integer' ? 1 : 'any'}
        min={typeof field.minimum === 'number' ? field.minimum : undefined} max={typeof field.maximum === 'number' ? field.maximum : undefined}
        minLength={typeof field.minLength === 'number' ? field.minLength : undefined} maxLength={typeof field.maxLength === 'number' ? field.maxLength : undefined}
        onChange={(event) => update(type === 'string' ? event.target.value : Number.isFinite(event.target.valueAsNumber) ? event.target.valueAsNumber : undefined)} />}
      {typeof field.description === 'string' ? <small>{field.description}</small> : null}
      {!required.includes(name) && value !== undefined ? <button type="button" disabled={disabled} onClick={() => update(undefined)}>{t('plugins.omit', 'Leave unset')}</button> : null}
    </Field>;
  })}</Fields>;
}
