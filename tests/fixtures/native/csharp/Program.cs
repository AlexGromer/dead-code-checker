using Microsoft.Extensions.DependencyInjection;

public static class Program
{
    public static void Main(string[] args)
    {
        var services = new ServiceCollection();
        services.AddScoped<IRepo, SqlRepo>();
        new Service(new SqlRepo()).Run();
    }
}
