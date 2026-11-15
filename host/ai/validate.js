/**
 * Small dependency-free Draft-07 subset validator.
 *
 * The Host plugin has no runtime npm dependencies, and M1.3a must stay
 * loadable in a plain Node process. This validator intentionally supports
 * only the keywords used by host/ai/schemas.js; unsupported keywords are
 * ignored, not silently treated as success by a JSON.parse-only path.
 */

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function deepEqual(a, b) {
  if (a === b) return true
  if (typeof a !== typeof b) return false
  if (a === null || b === null) return false
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false
    return a.every((item, index) => deepEqual(item, b[index]))
  }
  if (typeof a === 'object') {
    const ak = Object.keys(a)
    const bk = Object.keys(b)
    if (ak.length !== bk.length) return false
    return ak.every((key) => Object.prototype.hasOwnProperty.call(b, key) && deepEqual(a[key], b[key]))
  }
  return false
}

function typeMatches(value, type) {
  switch (type) {
    case 'null': return value === null
    case 'boolean': return typeof value === 'boolean'
    case 'string': return typeof value === 'string'
    case 'number': return typeof value === 'number' && Number.isFinite(value)
    case 'integer': return typeof value === 'number' && Number.isInteger(value)
    case 'array': return Array.isArray(value)
    case 'object': return isObject(value)
    default: return true
  }
}

function resolveRef(ref, root) {
  if (typeof ref !== 'string' || !ref.startsWith('#/')) {
    throw new Error(`Unsupported $ref: ${String(ref)}`)
  }
  return ref.slice(2).split('/').reduce((node, rawPart) => {
    const part = rawPart.replace(/~1/g, '/').replace(/~0/g, '~')
    if (node === null || node === undefined || !Object.prototype.hasOwnProperty.call(node, part)) {
      throw new Error(`Unresolvable $ref: ${ref}`)
    }
    return node[part]
  }, root)
}

function error(path, message, keyword = '') {
  return { path, message, keyword }
}

/**
 * Validate a value against a schema. Returns [] on success.
 * @param {object} schema
 * @param {unknown} value
 * @param {object} [root] root used to resolve local $ref
 * @param {string} [path]
 */
export function validateSchema(schema, value, root = schema, path = '$') {
  if (schema === true || schema === undefined) return []
  if (schema === false) return [error(path, 'schema is false', 'false')]
  if (!isObject(schema)) throw new Error('schema must be an object')

  if (schema.$ref !== undefined) {
    return validateSchema(resolveRef(schema.$ref, root), value, root, path)
  }

  if (Array.isArray(schema.allOf)) {
    return schema.allOf.flatMap((branch, index) => validateSchema(branch, value, root, `${path}<allOf:${index}>`))
  }

  if (Array.isArray(schema.anyOf)) {
    const results = schema.anyOf.map((branch, index) => validateSchema(branch, value, root, `${path}<anyOf:${index}>`))
    if (results.some((items) => items.length === 0)) return []
    return results.flat().slice(0, 20)
  }

  if (Array.isArray(schema.oneOf)) {
    const results = schema.oneOf.map((branch, index) => validateSchema(branch, value, root, `${path}<oneOf:${index}>`))
    const valid = results.filter((items) => items.length === 0).length
    if (valid === 1) return []
    if (valid > 1) return [error(path, 'matched more than one oneOf branch', 'oneOf')]
    return results.flat().slice(0, 20)
  }

  if (schema.const !== undefined && !deepEqual(schema.const, value)) {
    return [error(path, `must equal ${JSON.stringify(schema.const)}`, 'const')]
  }

  if (Array.isArray(schema.enum) && !schema.enum.some((candidate) => deepEqual(candidate, value))) {
    return [error(path, `must be one of ${schema.enum.map((v) => JSON.stringify(v)).join(', ')}`, 'enum')]
  }

  if (schema.type !== undefined) {
    const types = Array.isArray(schema.type) ? schema.type : [schema.type]
    if (!types.some((type) => typeMatches(value, type))) {
      return [error(path, `expected ${types.join('|')}`, 'type')]
    }
  }

  const errors = []

  if (typeof value === 'string') {
    if (schema.minLength !== undefined && value.length < schema.minLength) {
      errors.push(error(path, `length < ${schema.minLength}`, 'minLength'))
    }
    if (schema.maxLength !== undefined && value.length > schema.maxLength) {
      errors.push(error(path, `length > ${schema.maxLength}`, 'maxLength'))
    }
    if (schema.pattern !== undefined) {
      let re
      try { re = new RegExp(schema.pattern) } catch { re = null }
      if (re === null || !re.test(value)) errors.push(error(path, `does not match ${schema.pattern}`, 'pattern'))
    }
  }

  if (typeof value === 'number' && Number.isFinite(value)) {
    if (schema.minimum !== undefined && value < schema.minimum) errors.push(error(path, `must be >= ${schema.minimum}`, 'minimum'))
    if (schema.maximum !== undefined && value > schema.maximum) errors.push(error(path, `must be <= ${schema.maximum}`, 'maximum'))
    if (schema.exclusiveMinimum !== undefined && value <= schema.exclusiveMinimum) {
      errors.push(error(path, `must be > ${schema.exclusiveMinimum}`, 'exclusiveMinimum'))
    }
    if (schema.exclusiveMaximum !== undefined && value >= schema.exclusiveMaximum) {
      errors.push(error(path, `must be < ${schema.exclusiveMaximum}`, 'exclusiveMaximum'))
    }
  }

  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) errors.push(error(path, `items < ${schema.minItems}`, 'minItems'))
    if (schema.maxItems !== undefined && value.length > schema.maxItems) errors.push(error(path, `items > ${schema.maxItems}`, 'maxItems'))
    if (schema.items !== undefined) {
      value.forEach((item, index) => {
        errors.push(...validateSchema(schema.items, item, root, `${path}[${index}]`))
      })
    }
  }

  if (isObject(value)) {
    if (Array.isArray(schema.required)) {
      for (const key of schema.required) {
        if (!Object.prototype.hasOwnProperty.call(value, key)) {
          errors.push(error(path, `missing required property "${key}"`, 'required'))
        }
      }
    }
    const properties = isObject(schema.properties) ? schema.properties : {}
    for (const [key, child] of Object.entries(value)) {
      if (Object.prototype.hasOwnProperty.call(properties, key)) {
        errors.push(...validateSchema(properties[key], child, root, `${path}.${key}`))
      } else if (schema.additionalProperties === false) {
        errors.push(error(path, `unexpected property "${key}"`, 'additionalProperties'))
      } else if (isObject(schema.additionalProperties)) {
        errors.push(...validateSchema(schema.additionalProperties, child, root, `${path}.${key}`))
      }
    }
  }

  return errors
}

export function formatErrors(errors, limit = 12) {
  return errors.slice(0, limit).map((item) => `${item.path}: ${item.message}`).join('; ')
}

export function assertValid(schema, value, label = 'value') {
  const errors = validateSchema(schema, value)
  if (errors.length > 0) {
    const err = new Error(`${label} failed schema: ${formatErrors(errors)}`)
    err.code = 'SCHEMA_INVALID'
    err.validationErrors = errors
    throw err
  }
  return value
}
