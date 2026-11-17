import { Link } from 'react-router-dom'

export function NotFoundPage() {
  return (
    <div className="mx-auto max-w-md py-16 text-center">
      <h1 className="text-2xl font-semibold">Page not found</h1>
      <p className="mt-2 text-muted-foreground">This address does not match a page in Synth City.</p>
      <Link to="/" className="mt-6 inline-block text-primary underline-offset-4 hover:underline">
        Back to Overview
      </Link>
    </div>
  )
}
