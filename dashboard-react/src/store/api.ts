import { operatorSession } from '../auth/operatorSession';
import { createApi, fetchBaseQuery } from '@reduxjs/toolkit/query/react';

/**
 * Single RTK Query API slice for the dashboard. Endpoints are injected from
 * feature folders (`features/observability/api.ts`, etc.) using
 * `apiSlice.injectEndpoints` so each feature owns its own queries without
 * needing to edit one growing endpoints object.
 *
 * `tagTypes` enumerates every cache-invalidation tag used across the app. Add
 * to it when introducing a new entity; the type checker won't catch typos in
 * `providesTags` / `invalidatesTags` strings otherwise.
 */
export const apiSlice = createApi({
  reducerPath: 'api',
  baseQuery: fetchBaseQuery({ baseUrl: '/', fetchFn: operatorSession.fetch }),
  tagTypes: [
    'ClusterState',
    'Config',
    'ClusterTimeline',
    'TracingState',
    'TraceList',
    'Trace',
    'NodeDiagnostics',
    'PairingInvitations',
    'Plugins',
    'PluginConfiguration',
    'StewardStatus',
    'StewardProposals',
  ] as const,
  // Endpoints land via `injectEndpoints` from feature modules; this scaffold
  // intentionally has none so the migration can introduce them incrementally.
  endpoints: () => ({}),
});
