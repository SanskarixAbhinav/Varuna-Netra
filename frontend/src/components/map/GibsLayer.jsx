import { TileLayer } from "react-leaflet";

export const GibsLayer = ({ layer, date, template }) => {
  if (!layer || !date) return null;
  const url = template.replace("{layer}", layer.id).replace("{time}", date).replace("{matrix}", layer.matrix).replace("{ext}", layer.ext);
  const maxZ = +layer.matrix.replace(/\D/g, "") || 9;
  return <TileLayer key={`${layer.id}-${date}`} url={url} subdomains={["a", "b", "c"]} maxNativeZoom={maxZ} noWrap opacity={0.85} updateWhenIdle updateWhenZooming={false} keepBuffer={0} attribution="NASA GIBS / EOSDIS" />;
};
